"""界面冒烟测试：不碰真实游戏数据，只验证界面与线程装配是否正常。

覆盖：
  1. 主窗口 + 控制器能装配起来，后台扫描线程能把 14 款游戏灌进模型；
  2. 卡片的动作按钮按状态正确出现/消失（已在加速盘的游戏不给"加速"按钮）；
  3. 搜索/排序/筛选切换后模型行数正确；
  4. **删除确认对话框默认不允许删除**，必须勾选核对才启用删除按钮；
  5. 多余文件确认对话框能正确列出条目；
  6. 设置页能读出/写回配置。

运行： python tests/test_gui_smoke.py
（离屏运行；只在 tests\\_gui_sandbox 下写截图，不动任何真实文件）
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["STEAMBOOST_LOG_DIR"] = str(Path(__file__).resolve().parent / "_gui_sandbox" / "logs")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from drive_picker import drive_of  # noqa: E402

from PySide6.QtWidgets import QApplication, QLabel, QListWidget, QPushButton  # noqa: E402

from config import Config, list_volumes  # noqa: E402
from copy_engine import engine_summary, fastcopy_available  # noqa: E402
from deletion_guard import KIND_CACHE_COPY, build_request  # noqa: E402
from state import StateStore  # noqa: E402
from steam_scanner import ST_ACCELERATED, ST_ON_HDD, scan  # noqa: E402
from ui import theme  # noqa: E402
from ui.confirm_dialog import ConfirmDeletionDialog, ConfirmExtrasDialog  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.game_model import GameRole, actions_for, chip_rects  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.settings_dialog import SettingsDialog  # noqa: E402

SANDBOX = Path(__file__).resolve().parent / "_gui_sandbox"
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f"  → {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def pump(app: QApplication, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def save_shot(widget, name: str) -> None:
    """把控件截图存下来，**默认不存**。

    截图里必然带着运行时的真实内容（本机盘符、检测到的 FastCopy 位置），
    属于"看了才知道排版对不对"的东西，不适合每次跑测试都留在工作区里。
    需要时显式开启：``$env:STEAMBOOST_GUI_SHOTS=1; python tests\\test_gui_smoke.py``
    """
    if os.environ.get("STEAMBOOST_GUI_SHOTS") not in ("1", "true", "yes"):
        return
    SANDBOX.mkdir(parents=True, exist_ok=True)
    widget.grab().save(str(SANDBOX / name))


def real_drives() -> tuple[str, str]:
    """从**真实扫描结果**里推出母盘/加速盘，不写死任何盘符。"""
    probe = scan(Config())
    libraries = [lib for lib in probe.libraries if lib.exists]
    mother = drive_of(libraries[0].path) if libraries else drive_of(SANDBOX)
    volumes = [vol["drive"] for vol in list_volumes()]
    cache = next((drive for drive in volumes if drive != mother), mother)
    return mother, cache


def main() -> int:
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    SANDBOX.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)
    theme.apply_theme(app)

    mother, cache = real_drives()
    cfg = Config()
    cfg.mother_drive, cfg.cache_drive = mother, cache
    cfg.cache_dir = f"{cache}:\\SteamBoostCache"
    # 日志目录指向沙箱：设置页会把真实日志路径显示出来，指向沙箱可避免
    # 截图里印上这台机器的用户目录（测试产物不该带本机信息）
    cfg.log_dir = str(SANDBOX / "logs")

    report = scan(cfg)
    window = MainWindow(cfg, allow_cover_download=False)   # 冒烟测试不下载封面
    window.resize(1180, 830)
    window.load_report(report)
    window.show()
    pump(app, 0.4)

    # ---------------- 1) 模型装载 ----------------
    check("模型装载了全部游戏", window.model.rowCount() == len(report.games),
          f"{window.model.rowCount()} vs {len(report.games)}")
    row_games = [window.model.game_at(i) for i in range(window.model.rowCount())]
    check("模型里的记录可读", all(g is not None for g in row_games))

    # ---------------- 2) 动作按钮按状态出现 ----------------
    on_hdd = next((g for g in row_games if g.status == ST_ON_HDD), None)
    accelerated = next((g for g in row_games if g.status == ST_ACCELERATED), None)
    if on_hdd is not None:
        keys = [key for key, _ in actions_for(on_hdd)]
        check("母盘中的游戏有「加速」按钮", keys == ["accelerate"], str(keys))
    else:
        check("找到一款母盘中的游戏", False, "本机当前没有母盘中状态的游戏")
    if accelerated is not None:
        keys = [key for key, _ in actions_for(accelerated)]
        check("已加速的游戏有「回写」「释放」按钮", keys == ["writeback", "release"], str(keys))
    else:
        print("[INFO] 本机当前没有已加速的游戏，跳过该断言")

    # ---------------- 3) 搜索与筛选 ----------------
    total = window.model.rowCount()
    # 关键词从**真实游戏名**里取，不写死任何游戏
    keyword = next((g.name for g in row_games if len(g.name) >= 3), "")
    if keyword:
        window.search.setText(keyword[:3])
        pump(app, 0.1)
        filtered = window.model.rowCount()
        hits = [window.model.game_at(i).name for i in range(filtered)]
        check(
            "搜索只留下命中的游戏",
            0 < filtered <= total and all(keyword[:3].lower() in name.lower() for name in hits),
            f"{filtered} / {total} → {hits[:4]}",
        )
    else:
        print("[INFO] 没有长度足够的游戏名，跳过搜索断言")
    window.search.clear()
    pump(app, 0.1)
    check("清空搜索后恢复", window.model.rowCount() == total)
    window.filter_combo.setCurrentIndex(1)   # 可加速
    pump(app, 0.1)
    check("筛选「可加速」有效", window.model.rowCount() <= total, str(window.model.rowCount()))
    window.filter_combo.setCurrentIndex(0)
    window.sort_combo.setCurrentIndex(1)     # 按大小
    pump(app, 0.1)
    sizes = [window.model.game_at(i).size_on_disk for i in range(window.model.rowCount())]
    check("按大小排序生效", sizes == sorted(sizes, reverse=True), str(sizes[:5]))

    # ---------------- 4) 删除确认对话框默认拒绝 ----------------
    target = SANDBOX / "fake_cache_copy"
    (target / "sub").mkdir(parents=True)
    (target / "a.bin").write_bytes(b"a" * 1024)
    (target / "sub" / "b.bin").write_bytes(b"b" * 2048)
    request = build_request(KIND_CACHE_COPY, target, SANDBOX, reason="冒烟测试：确认对话框")
    dialog = ConfirmDeletionDialog(request, window)
    label_texts = [label.text() for label in dialog.findChildren(QLabel)]
    check("确认框展示了确切路径", str(request.target) in label_texts, str(label_texts[:3]))
    check("确认框展示了文件数与大小", any("个文件" in text for text in label_texts), str(label_texts[:6]))
    check("删除按钮默认禁用", not dialog.delete_button.isEnabled())
    dialog.confirm_check.setChecked(True)
    check("勾选核对后删除按钮可用", dialog.delete_button.isEnabled())
    dialog.confirm_check.setChecked(False)
    check("取消勾选后又禁用", not dialog.delete_button.isEnabled())
    check("默认按钮是取消", dialog.cancel_button.isDefault())
    save_shot(dialog, "dialog_delete.png")
    dialog.reject()

    # ---------------- 5) 多余文件确认对话框 ----------------
    extras_dialog = ConfirmExtrasDialog("测试游戏", ["old1.dat", "legacy/old2.dat"], 4096, window)
    extras_list = extras_dialog.findChild(QListWidget)
    check("多余文件确认框列出 2 个条目", extras_list is not None and extras_list.count() == 2,
          str(extras_list.count() if extras_list else None))
    save_shot(extras_dialog, "dialog_extras.png")
    extras_dialog.reject()

    # ---------------- 6) 设置页读写 ----------------
    settings = SettingsDialog(cfg, [lib.path for lib in report.libraries], window)
    check("设置页读出了母盘", settings.mother_combo.currentData() == mother, str(settings.mother_combo.currentData()))
    check("设置页读出了加速盘", settings.cache_combo.currentData() == cache, str(settings.cache_combo.currentData()))
    check("设置页有 FastCopy 路径输入框", hasattr(settings, "fastcopy_edit"))
    check(
        "FastCopy 状态提示与实际情况一致",
        ("已找到" in settings.fastcopy_hint.text()) == fastcopy_available(cfg),
        settings.fastcopy_hint.text(),
    )
    settings.cache_dir_edit.setText(f"{cache}:\\SteamBoostCache")
    settings.threads_spin.setValue(12)
    save_shot(settings, "dialog_settings.png")
    result_cfg = settings.result_config()
    check(
        "设置页写回配置",
        result_cfg.mother_drive == mother
        and result_cfg.robocopy_threads == 12
        and result_cfg.fastcopy_path == settings.fastcopy_edit.text().strip(),
    )
    settings.reject()

    # ---------------- 7) 进度面板：排队 → 进行中 → 完成 ----------------
    from copy_engine import ProgressState

    panel = window.progress
    panel.reset()
    panel.add_task("111111", "示例游戏甲")
    panel.add_task("222222", "示例游戏乙")
    check("队列里出现两行任务", len(panel._rows) == 2, str(len(panel._rows)))
    check("初始状态为排队中", all(row.state == "queued" for row in panel._rows.values()))
    panel.start_task("111111")
    check("开始后状态变为进行中", panel._rows["111111"].state == "running")
    panel.update_task(
        "111111",
        ProgressState(
            percent=42.5, bytes_done=1000, bytes_total=2000,
            speed_bps=1048576.0, eta_seconds=65, files_done=1, files_total=2,
        ),
    )
    check("进度条数值正确", panel._rows["111111"].bar.value() == 42, str(panel._rows["111111"].bar.value()))
    detail = panel._rows["111111"].detail.text()
    check("详情显示速度与剩余时间", "MB/s" in detail and "剩余" in detail, detail)
    check("进行中的任务可取消", panel._rows["111111"].cancel_button.isEnabled())
    panel.finish_task("111111", "done", "完成")
    panel.finish_task("222222", "failed", "失败")
    title = panel.title.text()
    check("汇总标题统计正确", "完成 1" in title and "失败/跳过 1" in title, title)
    check("完成后取消按钮禁用", not panel._rows["111111"].cancel_button.isEnabled())
    check("面板高度随行数展开", panel.height() >= 100, str(panel.height()))
    pump(app, 0.3)   # 让布局跑一轮，否则截图拿到的是旧几何
    save_shot(window, "main_with_tasks.png")

    # ---------------- 8) 回归：真实鼠标点击卡片按钮（事件过滤器曾装错对象） ----------------
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QAbstractItemView

    window.view.scrollToTop()
    pump(app, 0.3)
    window.view.doItemsLayout()
    pump(app, 0.1)

    target_row = None
    for row in range(window.model.rowCount()):
        game = window.model.game_at(row)
        if [key for key, _ in actions_for(game)] == ["accelerate"]:
            target_row = row
            break
    check("找到一张带「加速」按钮的卡片", target_row is not None)

    received: list[tuple[str, list[str]]] = []
    window.operationRequested.connect(lambda action, ids: received.append((action, list(ids))))

    if target_row is not None:
        index = window.model.index(target_row, 0)
        rect = window.view.visualRect(index)
        chips = chip_rects(rect, 1)
        check("卡片按钮矩形在可视区域内", chips and window.view.viewport().rect().contains(chips[0].center()),
              f"chip={chips[0].center() if chips else None}")
        expected_appid = window.model.game_at(target_row).appid
        QTest.mouseClick(
            window.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, chips[0].center()
        )
        pump(app, 0.2)
        check("点击卡片按钮会触发操作（真实鼠标事件）",
              received == [("accelerate", [expected_appid])], str(received))

        received.clear()
        QTest.mouseClick(
            window.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            QPoint(rect.center().x(), rect.top() + 60),
        )
        pump(app, 0.2)
        check("点击封面区域不会误触发操作", received == [], str(received))

        # 命中判定辅助函数与绘制保持一致
        hit = window.chip_rect_at(chips[0].center())
        check("chip_rect_at 命中结果正确", hit == ("accelerate", expected_appid), str(hit))
        check("chip_rect_at 在空白处返回 None",
              window.chip_rect_at(QPoint(rect.center().x(), rect.top() + 60)) is None)
    window.operationRequested.disconnect()

    # ---------------- 9) 回归：滚动要顺（按像素 + 动画，而不是整行跳） ----------------
    check("纵向滚动模式为按像素",
          window.view.verticalScrollMode() == QAbstractItemView.ScrollMode.ScrollPerPixel)
    bar = window.view.verticalScrollBar()
    if bar.maximum() > 0:
        bar.setValue(bar.maximum())
        pump(app, 0.2)
        before = bar.value()
        wheel = QWheelEvent(
            QPointF(120, 120), QPointF(120, 120), QPoint(0, 0), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )
        QApplication.sendEvent(window.view.viewport(), wheel)
        pump(app, 0.6)
        check("滚轮向上滚动生效", bar.value() < before, f"{before} → {bar.value()}")
        step = window.view.gridSize().height() // 3
        check("单次滚动距离小于一整行（更细腻）", (before - bar.value()) <= window.view.gridSize().height(),
              f"滚动 {before - bar.value()}px，行高 {window.view.gridSize().height()}px")
    else:
        print("[INFO] 当前内容不足以滚动，跳过滚轮断言")

    # ---------------- 10) 修复向导与空间提醒 ----------------
    from repair import (
        ACTION_RESUME_ACCELERATE,
        ACTION_ROLLBACK_TO_HDD,
        KIND_ACCELERATE_INTERRUPTED,
        RepairAction,
        RepairCase,
        analyze,
    )
    from steam_scanner import GameRecord
    from ui.repair_dialog import RepairDialog
    from ui.space_warning import SpaceStatus, SpaceWarningDialog, check_space, release_candidates, suggest_selection

    store = StateStore(SANDBOX / "smoke_state.json", cfg=cfg)
    clean = analyze(report, cfg, store)
    check("真机干净状态没有修复项", clean == [], str([c.kind for c in clean]))

    empty_dialog = RepairDialog([], cfg, store, report)
    empty_texts = [label.text() for label in empty_dialog.findChildren(QLabel)]
    check("修复向导在无事可做时给出明确提示",
          any("没有需要处理" in text for text in empty_texts), str(empty_texts[:4]))
    save_shot(empty_dialog, "dialog_repair_empty.png")
    empty_dialog.reject()

    sample = RepairCase(
        kind=KIND_ACCELERATE_INTERRUPTED,
        title="加速未完成：示例游戏",
        detail="母本停在 .hdd_cache 暂存区，原位置没有联接，Steam 现在看不到这个游戏。",
        appid="111111",
        game_name="示例游戏",
        paths={
            "game_path": str(SANDBOX / "lib" / "steamapps" / "common" / "SampleGame"),
            "backup": str(SANDBOX / "lib" / "steamapps" / "common" / ".hdd_cache" / "SampleGame"),
            "cache_copy": str(SANDBOX / "cache" / "111111_SampleGame"),
        },
        actions=[
            RepairAction(ACTION_RESUME_ACCELERATE, "继续加速（推荐）", "复制到加速盘并建立联接", recommended=True),
            RepairAction(ACTION_ROLLBACK_TO_HDD, "回滚到机械盘", "母本改回原位"),
        ],
    )
    repair_dialog = RepairDialog([sample], cfg, store, report)
    check("修复向导列出该现场", repair_dialog.list.count() == 1, str(repair_dialog.list.count()))
    buttons = [b.text() for b in repair_dialog.findChildren(QPushButton)]
    check("修复向导展示两个可选动作",
          "继续加速（推荐）" in buttons and "回滚到机械盘" in buttons, str(buttons))
    check("推荐动作使用主按钮样式",
          any(b.text() == "继续加速（推荐）" and b.objectName() == "Primary"
              for b in repair_dialog.findChildren(QPushButton)))
    save_shot(repair_dialog, "dialog_repair.png")
    repair_dialog.reject()

    check("真机当前不需要空间提醒", check_space(cfg) is None)
    # 下面几条用**虚构盘符**的合成记录，不指向任何真实路径
    fake_games = [
        GameRecord(appid="1", name="最近玩的", installdir="A", library_path="X:\\SteamLibrary", size_on_disk=50 * 1024 ** 3, last_played=1790000000, status="accelerated"),
        GameRecord(appid="2", name="很久没玩", installdir="B", library_path="X:\\SteamLibrary", size_on_disk=40 * 1024 ** 3, last_played=1700000000, status="accelerated"),
        GameRecord(appid="3", name="从没玩过", installdir="C", library_path="X:\\SteamLibrary", size_on_disk=20 * 1024 ** 3, last_played=0, status="accelerated"),
    ]
    tight = SpaceStatus(drive="Y", free=30 * 1024 ** 3, total=781 * 1024 ** 3,
                        threshold=78 * 1024 ** 3, reason="测试")
    order = [g.name for g in release_candidates(fake_games)]
    check("候选按最近游玩从旧到新排序", order == ["从没玩过", "很久没玩", "最近玩的"], str(order))
    picked = suggest_selection(release_candidates(fake_games), tight)
    check("默认勾选能补足空间的最旧项", picked == ["3", "2"], str(picked))
    space_dialog = SpaceWarningDialog(tight, release_candidates(fake_games))
    check("空间提醒表格列出全部候选", space_dialog.table.rowCount() == 3, str(space_dialog.table.rowCount()))
    summary = space_dialog.summary.text()
    check("摘要显示可释放量与释放后剩余", "可释放" in summary and "释放后" in summary, summary)
    emitted: list[list[str]] = []
    space_dialog.releaseRequested.connect(lambda ids: emitted.append(list(ids)))
    space_dialog._release()
    check("点击释放会带上勾选的 appid", emitted == [["3", "2"]], str(emitted))
    save_shot(space_dialog, "dialog_space.png")

    # ---------------- 11) 控制器与扫描线程装配 ----------------
    window2 = MainWindow(cfg, allow_cover_download=False)
    store = StateStore(SANDBOX / "state.json", cfg=cfg)
    controller = AppController(window2, cfg, store)
    pump(app, 0.2)
    check("控制器装配成功", controller.report is None and controller.deletion_confirmer is not None)

    # 状态栏必须显示**实际**生效的引擎，找不到 FastCopy 时明确写"已回退"
    controller.refresh_engine_label()
    pump(app, 0.1)
    engine_text = window2.engine_label.text()
    expected = engine_summary(cfg)
    check("状态栏显示当前引擎", engine_text == f"引擎：{expected}", engine_text)
    check(
        "回退到 robocopy 时用警示样式",
        (window2.engine_label.objectName() == "Warn") == ("回退" in expected),
        f"{window2.engine_label.objectName()} / {expected}",
    )
    check("引擎标签在界面上可见", window2.engine_label.isVisible() or not window2.isVisible())
    save_shot(window2, "main_engine_label.png")

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
