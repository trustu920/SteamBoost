"""控制层：把界面上的意图接到真正的操作上。

职责边界很清晰——
* 界面（``MainWindow`` / 各对话框）只管展示与收集意图；
* 本控制器决定"能不能做、按什么顺序做、结果怎么反馈"；
* 真正的文件操作在 :mod:`operations`，删除一律经 :mod:`deletion_guard` 闸门。

所有可能耗时的工作都丢进 :mod:`ui.workers` 的线程，UI 线程只更新控件。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QDialog, QMessageBox

from config import Config, human_size
from copy_engine import engine_summary
from quarantine import list_items, purge_item, restore_item
from repair import analyze
from state import StateStore
from steam_scanner import ACCELERATABLE, RELEASABLE, GameRecord, ScanReport
from ui.confirm_dialog import ConfirmDeletionDialog, ConfirmExtrasDialog
from ui.main_window import MainWindow
from ui.quarantine_dialog import QuarantineDialog
from ui.repair_dialog import RepairDialog
from ui.settings_dialog import SettingsDialog
from ui.space_warning import SpaceWarningDialog, check_space, release_candidates
from ui.workers import GuiDeletionConfirmer, GuiExtrasConfirmer, OperationWorker, ScanWorker

ACTION_LABELS = {
    "accelerate": "加速到 SSD",
    "writeback": "回写母盘",
    "release": "释放 SSD 空间",
    "repair": "修复",
}


class AppController(QObject):
    """应用控制器。"""

    def __init__(self, window: MainWindow, cfg: Config, store: StateStore) -> None:
        super().__init__(window)
        self.window = window
        self.cfg = cfg
        self.store = store
        self.report: ScanReport | None = None
        self.scan_worker: ScanWorker | None = None
        self.op_worker: OperationWorker | None = None
        self.results: list[str] = []

        # 确认者住在 UI 线程；工作线程会阻塞等它们弹框的结果
        self.deletion_confirmer = GuiDeletionConfirmer(
            lambda request: ConfirmDeletionDialog(request, self.window), self.window
        )
        self.extras_confirmer = GuiExtrasConfirmer(
            lambda name, extras, total: ConfirmExtrasDialog(name, extras, total, self.window),
            self.window,
        )

        window.scanRequested.connect(self.rescan)
        window.operationRequested.connect(self.run_operation)
        window.settingsRequested.connect(self.open_settings)
        window.quarantineRequested.connect(self.open_quarantine)
        window.repairRequested.connect(self.open_repair)
        # 逐任务的取消按钮（只有"加速"阶段可取消；回写阶段按设计不可取消）
        window.progress.taskCancelRequested.connect(lambda _appid: self.cancel_operation())

        # 加速盘空间提醒：启动后与定时都检查（5 分钟一次）
        self.repair_cases: list = []
        self._repair_prompted = False
        self._space_snoozed = False
        self.space_timer = QTimer(self)
        self.space_timer.setInterval(5 * 60 * 1000)
        self.space_timer.timeout.connect(self.check_space)
        self.space_timer.start()

    # ------------------------------------------------------------ 扫描
    def start(self) -> None:
        self.refresh_engine_label()
        self.rescan()

    def refresh_engine_label(self) -> None:
        """状态栏显示**实际**会用的复制引擎（未找到 FastCopy 时明确说"已回退"）。"""
        try:
            self.window.set_engine_text(engine_summary(self.cfg))
        except Exception as exc:  # noqa: BLE001 - 只是状态显示，绝不能因此崩掉界面
            self.window.set_engine_text(f"检测失败（{exc}）")

    def rescan(self) -> None:
        if self.scan_worker is not None and self.scan_worker.isRunning():
            return
        self.window.set_status("正在扫描 Steam 库…")
        self.window.refresh_button.setEnabled(False)
        worker = ScanWorker(self.cfg, self.store, self.window)
        worker.finishedOk.connect(self._on_scan_done)
        worker.failed.connect(self._on_scan_failed)
        worker.finished.connect(lambda: self.window.refresh_button.setEnabled(True))
        self.scan_worker = worker
        worker.start()

    def _on_scan_done(self, report: ScanReport) -> None:
        self.report = report
        self.window.load_report(report)
        self.window.set_status(
            f"扫描完成：{len(report.games)} 款游戏，{len(report.libraries)} 个库"
        )
        self.refresh_repair_cases(report)
        self.check_space()

    def _on_scan_failed(self, message: str) -> None:
        self.window.set_status(f"扫描失败：{message}")
        QMessageBox.warning(self.window, "扫描失败", f"读取 Steam 信息时出错：\n{message}")

    # ------------------------------------------------------------ 操作
    def _games_for(self, appids: list[str]) -> tuple[list[GameRecord], list[str]]:
        """按 appid 从最近一次扫描里取出游戏记录，并筛掉状态不允许的。"""
        if self.report is None:
            return [], list(appids)
        by_appid = {game.appid: game for game in self.report.games}
        picked: list[GameRecord] = []
        skipped: list[str] = []
        action = self._pending_action
        for appid in appids:
            game = by_appid.get(appid)
            if game is None:
                skipped.append(f"{appid}（未找到）")
                continue
            if action == "accelerate" and game.status not in ACCELERATABLE:
                skipped.append(f"{game.name}（当前 {game.status_label}）")
                continue
            if action in ("writeback", "release") and game.status not in RELEASABLE:
                skipped.append(f"{game.name}（当前 {game.status_label}）")
                continue
            if action == "repair":
                skipped.append(f"{game.name}（请使用修复向导）")
                continue
            picked.append(game)
        return picked, skipped

    def run_operation(self, action: str, appids: list[str]) -> None:
        if self.op_worker is not None and self.op_worker.isRunning():
            QMessageBox.information(self.window, "请稍候", "已有任务在执行，请等待它完成。")
            return
        self._pending_action = action
        games, skipped = self._games_for(appids)
        if skipped:
            QMessageBox.information(
                self.window,
                "部分游戏已跳过",
                "以下游戏当前状态不支持该操作，已跳过：\n\n" + "\n".join(skipped[:12]),
            )
        if not games:
            return

        label = ACTION_LABELS.get(action, action)
        if action == "release":
            total = sum(game.size_on_disk for game in games)
            answer = QMessageBox.question(
                self.window,
                "确认释放",
                f"即将释放 {len(games)} 款游戏，预计回收约 {human_size(total)} 的加速盘空间。\n\n"
                "释放流程：回写母盘 → 删除目录联接 → 母本回到原位 → 删除缓存副本。\n"
                "删除缓存副本前还会单独弹窗让你核对确切路径。\n\n是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.results = []
        self.window.refresh_button.setEnabled(False)
        worker = OperationWorker(
            action, games, self.cfg, self.store,
            deletion_confirmer=self.deletion_confirmer,
            extras_confirmer=self.extras_confirmer,
            parent=self.window,
        )
        worker.progressed.connect(self._on_progress)
        worker.gameStarted.connect(self._on_game_started)
        worker.gameFinished.connect(self._on_game_finished)
        worker.gameBlocked.connect(self._on_game_blocked)
        worker.gameFailed.connect(self._on_game_failed)
        worker.allFinished.connect(self._on_all_finished)
        self.op_worker = worker

        # 把整批任务先列进队列面板（排队中 → 进行中 → 完成/失败）
        cancellable = action == "accelerate"
        for game in games:
            self.window.progress.add_task(game.appid, game.name, cancellable=cancellable)
        self.window.show_progress(f"{label}：已加入队列（{len(games)} 个）")
        worker.start()

    def cancel_operation(self) -> None:
        if self.op_worker is not None and self.op_worker.isRunning():
            if self._pending_action != "accelerate":
                QMessageBox.information(
                    self.window, "无法取消",
                    "回写阶段不允许取消：中途停下会让母盘处于半同步状态，难以判断哪边权威。\n"
                    "请等它跑完（通常只要几秒到几十秒）。",
                )
                return
            self.op_worker.cancel()
            self.window.set_status("已请求取消，正在终止复制并回滚…")

    def _on_game_started(self, appid: str, name: str) -> None:
        self._current_appid = appid
        self.window.set_status(f"正在处理：{name}")
        self.window.progress.start_task(appid)

    def _on_progress(self, progress) -> None:
        appid = getattr(self, "_current_appid", "")
        if appid:
            self.window.progress.update_task(appid, progress)

    def _on_game_finished(self, result) -> None:
        self.results.append("✓ " + result.message)
        self.window.progress.finish_task(result.appid, "done", result.message)

    def _on_game_blocked(self, payload) -> None:
        appid, exc = payload
        blockers = "；".join(exc.blockers[:2])
        self.results.append(f"✗ {appid} 未执行：{blockers}")
        self.window.progress.finish_task(appid, "blocked", blockers or "已跳过")

    def _on_game_failed(self, payload) -> None:
        appid, exc = payload
        detail = getattr(exc, "detail", "")
        text = str(exc) + (f"（{detail}）" if detail else "")
        self.results.append(f"✗ {appid} 失败：{text}")
        self.window.progress.finish_task(appid, "failed", text)

    def _on_all_finished(self, ok_count: int, fail_count: int) -> None:
        self.window.show_progress(f"本批完成：成功 {ok_count}，失败 {fail_count}（可点「清除已完成」）")
        self.window.refresh_button.setEnabled(True)
        if self.results:
            QMessageBox.information(self.window, "执行结果", "\n".join(self.results[:20]))
        self.rescan()

    # ------------------------------------------------------------ 设置
    def open_settings(self) -> None:
        libraries = [lib.path for lib in self.report.libraries] if self.report else []
        dialog = SettingsDialog(self.cfg, libraries, self.window)
        dialog.pruneLogsRequested.connect(lambda: self.prune_logs(dialog))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_cfg = dialog.result_config()
        problems, warnings = new_cfg.validate(
            [(lib, lib[:2].rstrip(":\\").upper()) for lib in libraries]
        )
        if problems:
            QMessageBox.warning(
                self.window, "设置未保存",
                "以下问题必须先解决：\n\n" + "\n".join(f"· {item}" for item in problems),
            )
            return
        if warnings:
            QMessageBox.information(
                self.window, "提醒", "\n".join(f"· {item}" for item in warnings[:6])
            )
        new_cfg.save()
        self.cfg = new_cfg
        self.window.cfg = new_cfg
        self.window._update_drive_bars()
        self.refresh_engine_label()
        self.window.set_status("设置已保存")
        self.rescan()

    def prune_logs(self, dialog: SettingsDialog) -> None:
        from logger import prune_logs

        freed = prune_logs(keep_days=30, confirmer=self.deletion_confirmer, cfg=self.cfg)
        dialog.refresh_log_usage()
        QMessageBox.information(self.window, "日志清理", f"已释放 {human_size(freed)}")

    # ------------------------------------------------------------ 隔离区
    def open_quarantine(self) -> None:
        dialog = QuarantineDialog(list_items(self.cfg.mother_drive), self.window)
        dialog.purgeRequested.connect(self._purge_quarantine)
        dialog.restoreRequested.connect(self._restore_quarantine)
        dialog.exec()
        self.rescan()

    def _purge_quarantine(self, item_dir: str) -> None:
        try:
            freed = purge_item(item_dir, self.cfg.mother_drive, self.deletion_confirmer)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self.window, "未删除", str(exc))
            return
        QMessageBox.information(self.window, "已删除", f"释放 {human_size(freed)}")

    def _restore_quarantine(self, item_dir: str) -> None:
        try:
            restored = restore_item(item_dir, self.cfg.mother_drive)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self.window, "还原失败", str(exc))
            return
        QMessageBox.information(self.window, "已还原", f"还原了 {restored} 个文件")

    # ------------------------------------------------------------ 修复向导
    def refresh_repair_cases(self, report: ScanReport | None = None) -> int:
        """重新分析需要修复的现场，并更新按钮提示。"""
        target = report or self.report
        if target is None:
            return 0
        self.repair_cases = analyze(target, self.cfg, self.store)
        count = len(self.repair_cases)
        self.window.repair_button.setText(f"修复向导 ({count})" if count else "修复向导")
        if count and not self._repair_prompted:
            # 启动自检命中：自动把向导推到用户面前（只自动一次，避免打扰）
            self._repair_prompted = True
            QTimer.singleShot(400, self.open_repair)
        return count

    def open_repair(self) -> None:
        if self.report is None:
            return
        cases = analyze(self.report, self.cfg, self.store)
        dialog = RepairDialog(
            cases, self.cfg, self.store, self.report,
            deletion_confirmer=self.deletion_confirmer,
            extras_confirmer=self.extras_confirmer,
            parent=self.window,
        )
        dialog.repaired.connect(lambda _message: self.rescan())
        dialog.exec()
        self.rescan()

    # ------------------------------------------------------------ 空间提醒
    def check_space(self) -> None:
        """加速盘剩余空间低于阈值时提醒（列出最该释放的游戏）。"""
        if self._space_snoozed or self.report is None:
            return
        status = check_space(self.cfg)
        if status is None:
            return
        candidates = release_candidates(self.report.games)
        if not candidates:
            self.window.set_status(f"{status.describe()}（当前没有可释放的加速游戏）")
            return
        dialog = SpaceWarningDialog(status, candidates, self.window)
        dialog.snoozeRequested.connect(self._snooze_space)
        dialog.releaseRequested.connect(lambda appids: self.run_operation("release", appids))
        dialog.exec()

    def _snooze_space(self) -> None:
        self._space_snoozed = True
        self.window.set_status("已设置：本次运行不再提醒加速盘空间")
