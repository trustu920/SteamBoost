"""后台线程与 GUI 确认者。

线程模型（UI 线程永不冻结）
--------------------------
* :class:`ScanWorker` —— 扫描 Steam 库（读注册表/vdf/acf/文件系统），跑在子线程；
* :class:`OperationWorker` —— 执行加速/回写/释放，可批量（**串行队列**，一次动一个游戏，
  磁盘 IO 串行更稳，也让进度显示清晰），通过信号回报进度与结果。

跨线程"要人确认"怎么实现
------------------------
删除确认必须由**用户**在界面点，但发起删除的是工作线程。做法：
确认者对象住在 UI 线程，工作线程调用它的 ``confirm()`` 时——
emits 一个信号（Qt 自动排队到 UI 线程）→ UI 弹对话框 → 把结果写回并唤醒工作线程。
工作线程在等待期间一直阻塞，因此**没点确认就绝不会继续删**。
"""

from __future__ import annotations

import threading
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from config import Config
from deletion_guard import DeletionRequest
from operations import (
    OperationBlocked,
    OperationFailed,
    accelerate,
    release,
    writeback,
)
from state import StateStore
from steam_scanner import scan

#: 等待用户确认的最长时间（秒）；超时视为拒绝
CONFIRM_TIMEOUT = 900


class ScanWorker(QThread):
    """后台扫描。"""

    finishedOk = Signal(object)   # ScanReport
    failed = Signal(str)

    def __init__(self, cfg: Config, state: StateStore, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.state = state

    def run(self) -> None:  # noqa: D102
        try:
            report = scan(self.cfg, state=self.state.to_dict())
        except Exception as exc:  # noqa: BLE001 - 任何异常都要回到界面而不是崩掉
            self.failed.emit(str(exc))
            return
        self.finishedOk.emit(report)


class OperationWorker(QThread):
    """后台执行一个或多个游戏的操作（串行）。"""

    progressed = Signal(object)   # ProgressState
    gameStarted = Signal(str, str)  # appid, 名称
    gameFinished = Signal(object)   # OperationResult
    gameBlocked = Signal(object)    # (appid, OperationBlocked)
    gameFailed = Signal(object)     # (appid, OperationFailed)
    allFinished = Signal(int, int)  # 成功数, 失败数

    def __init__(
        self,
        action: str,
        games: list[Any],
        cfg: Config,
        store: StateStore,
        deletion_confirmer: Any = None,
        extras_confirmer: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.action = action
        self.games = list(games)
        self.cfg = cfg
        self.store = store
        self.deletion_confirmer = deletion_confirmer
        self.extras_confirmer = extras_confirmer
        self.cancel_event = threading.Event()
        self.current_appid = ""

    def cancel(self) -> None:
        """请求取消（仅"加速"阶段有效；回写阶段按设计不可取消）。"""
        self.cancel_event.set()

    def run(self) -> None:  # noqa: D102
        ok_count = 0
        fail_count = 0
        for game in self.games:
            if self.cancel_event.is_set() and self.action == "accelerate":
                break
            self.current_appid = game.appid
            self.gameStarted.emit(game.appid, game.name)
            try:
                if self.action == "accelerate":
                    result = accelerate(
                        game,
                        self.cfg,
                        store=self.store,
                        on_progress=self.progressed.emit,
                        cancel_event=self.cancel_event,
                        deletion_confirmer=self.deletion_confirmer,
                    )
                elif self.action == "writeback":
                    result = writeback(
                        game,
                        self.cfg,
                        store=self.store,
                        on_progress=self.progressed.emit,
                        extras_confirmer=self.extras_confirmer,
                    )
                elif self.action == "release":
                    result = release(
                        game,
                        self.cfg,
                        store=self.store,
                        on_progress=self.progressed.emit,
                        deletion_confirmer=self.deletion_confirmer,
                        extras_confirmer=self.extras_confirmer,
                    )
                else:
                    raise OperationFailed(f"未知操作：{self.action}")
            except OperationBlocked as exc:
                fail_count += 1
                self.gameBlocked.emit((game.appid, exc))
                continue
            except OperationFailed as exc:
                fail_count += 1
                self.gameFailed.emit((game.appid, exc))
                continue
            except Exception as exc:  # noqa: BLE001
                fail_count += 1
                self.gameFailed.emit(
                    (game.appid, OperationFailed(f"未预期的错误：{exc}", detail="请查看日志"))
                )
                continue
            ok_count += 1
            self.gameFinished.emit(result)
        self.allFinished.emit(ok_count, fail_count)


class RepairWorker(QThread):
    """后台执行一个修复动作（"继续加速"可能要复制几十 GB，不能卡住界面）。"""

    finishedOk = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        case,
        action_key: str,
        cfg: Config,
        store: StateStore,
        report=None,
        deletion_confirmer: Any = None,
        extras_confirmer: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.case = case
        self.action_key = action_key
        self.cfg = cfg
        self.store = store
        self.report = report
        self.deletion_confirmer = deletion_confirmer
        self.extras_confirmer = extras_confirmer

    def run(self) -> None:  # noqa: D102
        from repair import apply  # 延迟导入，避免与界面层循环依赖

        try:
            message = apply(
                self.case,
                self.action_key,
                self.cfg,
                self.store,
                report=self.report,
                deletion_confirmer=self.deletion_confirmer,
                extras_confirmer=self.extras_confirmer,
            )
        except Exception as exc:  # noqa: BLE001 - 任何失败都要回到界面
            self.failed.emit(str(exc))
            return
        self.finishedOk.emit(message)


class GuiDeletionConfirmer(QObject):
    """把删除确认交给界面：工作线程阻塞等待用户点击。

    ``confirm()`` 可能被工作线程调用，也可能被 UI 线程调用，两种都支持。
    """

    name = "gui-dialog"
    _requested = Signal(object)

    def __init__(self, dialog_factory, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._dialog_factory = dialog_factory
        self._event = threading.Event()
        self._result = False
        self._requested.connect(self._show_in_ui)

    def confirm(self, request: DeletionRequest) -> bool:
        if QThread.currentThread() is self.thread():
            return self._show_modal(request)
        self._event.clear()
        self._result = False
        self._requested.emit(request)
        if not self._event.wait(timeout=CONFIRM_TIMEOUT):
            return False
        return self._result

    def _show_in_ui(self, request: DeletionRequest) -> None:
        self._result = self._show_modal(request)
        self._event.set()

    def _show_modal(self, request: DeletionRequest) -> bool:
        try:
            dialog = self._dialog_factory(request)
        except Exception:  # noqa: BLE001 - 界面出错按拒绝处理（默认拒绝）
            return False
        from PySide6.QtWidgets import QDialog

        return dialog.exec() == QDialog.DialogCode.Accepted


class GuiExtrasConfirmer(QObject):
    """回写前"母盘多余文件搬入隔离区"的确认（同样是阻塞式问界面）。"""

    name = "gui-extras"
    _requested = Signal(str, object, int)

    def __init__(self, dialog_factory, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._dialog_factory = dialog_factory
        self._event = threading.Event()
        self._result = False
        self._requested.connect(self._show_in_ui)

    def confirm_extras(self, game_name: str, extras: list[str], total_bytes: int) -> bool:
        if QThread.currentThread() is self.thread():
            return self._show_modal(game_name, extras, total_bytes)
        self._event.clear()
        self._result = False
        self._requested.emit(game_name, list(extras), int(total_bytes))
        if not self._event.wait(timeout=CONFIRM_TIMEOUT):
            return False
        return self._result

    def _show_in_ui(self, game_name: str, extras: list[str], total_bytes: int) -> None:
        self._result = self._show_modal(game_name, extras, total_bytes)
        self._event.set()

    def _show_modal(self, game_name: str, extras: list[str], total_bytes: int) -> bool:
        try:
            dialog = self._dialog_factory(game_name, extras, total_bytes)
        except Exception:  # noqa: BLE001
            return False
        from PySide6.QtWidgets import QDialog

        return dialog.exec() == QDialog.DialogCode.Accepted
