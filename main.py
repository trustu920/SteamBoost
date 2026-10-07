"""SteamBoost 主程序入口。

职责：应用初始化、单实例、浅色主题、系统托盘、窗口与控制器的装配。

运行： python main.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon  # noqa: E402

from config import Config  # noqa: E402
from logger import setup_logger  # noqa: E402
from state import StateStore  # noqa: E402
from ui import theme  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.icons import app_icon  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

SINGLE_INSTANCE_KEY = "SteamBoost.SingleInstance.v1"


def make_icon():
    """程序图标（实现在 :mod:`ui.icons`，与窗口左上角共用同一份绘制）。"""
    return app_icon()


def notify_existing_instance() -> bool:
    """若已有实例在运行，通知它显示窗口并返回 True。"""
    socket = QLocalSocket()
    socket.connectToServer(SINGLE_INSTANCE_KEY)
    if socket.waitForConnected(300):
        socket.write(b"show")
        socket.flush()
        socket.waitForBytesWritten(300)
        socket.disconnectFromServer()
        return True
    return False


def run_selftest() -> int:
    """打印环境自检信息后退出。

    打包成 exe 之后没法直接看源码，这个入口用来验证
    "exe 能启动、依赖齐全、能读到 Steam 与复制引擎"。
    因为 GUI 版 exe 没有控制台，输出同时写入 ``<配置目录>\\selftest.log``。
    """
    lines: list[str] = []

    def say(text: str = "") -> None:
        lines.append(text)
        print(text)

    say("SteamBoost 自检")
    say(f"  运行方式   : {'打包 exe' if getattr(sys, 'frozen', False) else '源码'}")
    say(f"  Python     : {sys.version.split()[0]}")
    say(f"  可执行文件 : {sys.executable}")
    try:
        import PySide6

        say(f"  PySide6    : {PySide6.__version__}")
    except Exception as exc:  # noqa: BLE001
        say(f"  PySide6    : 导入失败（{exc}）")

    from config import app_data_dir

    cfg = Config.load()
    data_dir = app_data_dir()
    say(f"  配置目录   : {data_dir}")
    say(f"  母盘/加速盘 : {cfg.mother_drive or '未选择'} / {cfg.cache_drive or '未选择'}")
    say(f"  缓存目录   : {cfg.resolved_cache_dir()}")

    from copy_engine import engine_summary, find_fastcopy

    fastcopy = find_fastcopy(cfg.fastcopy_path)
    say(f"  复制引擎   : {engine_summary(cfg)}")
    say(f"  FastCopy   : {fastcopy or '未找到'}")

    from steam_scanner import find_steam_root, scan

    say(f"  Steam 根目录 : {find_steam_root() or '未找到'}")
    try:
        report = scan(cfg)
        say(f"  游戏数     : {len(report.games)}（库 {len(report.libraries)} 个，自检提示 {len(report.anomalies)} 条）")
    except Exception as exc:  # noqa: BLE001
        say(f"  扫描失败   : {exc}")
    say("自检完成")

    # 写自检日志：配置目录优先；若被系统策略挡住（受限账户 / 安全软件），
    # 退到程序目录，再退到临时目录——并把**真实路径**打出来，绝不静默失败。
    import tempfile

    candidates = [
        data_dir / "selftest.log",
        Path(sys.executable).resolve().parent / "selftest.log",
        Path(tempfile.gettempdir()) / "SteamBoost_selftest.log",
    ]
    errors: list[str] = []
    for target in candidates:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            errors.append(f"{target}（{exc.strerror or exc}）")
            continue
        say(f"  自检日志   : {target}")
        return 0
    say("  自检日志   : 写入失败 —— " + "；".join(errors))
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv
    if "--selftest" in arguments:
        return run_selftest()

    app = QApplication(arguments)
    app.setApplicationName("SteamBoost")
    app.setApplicationDisplayName("SteamBoost")
    theme.apply_theme(app)
    app.setWindowIcon(make_icon())

    cfg = Config.load()
    setup_logger("steamboot", cfg)

    if notify_existing_instance():
        print("SteamBoost 已在运行，已通知现有窗口显示。")
        return 0

    server = QLocalServer()
    QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
    server.listen(SINGLE_INSTANCE_KEY)

    store = StateStore(cfg=cfg)
    window = MainWindow(cfg)
    controller = AppController(window, cfg, store)

    tray: QSystemTrayIcon | None = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = QSystemTrayIcon(make_icon(), app)
        menu = QMenu()
        show_action = QAction("显示主窗口", menu)
        show_action.triggered.connect(lambda: (window.showNormal(), window.raise_(), window.activateWindow()))
        quit_action = QAction("退出", menu)
        quit_action.triggered.connect(app.quit)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.setToolTip("SteamBoost")
        tray.activated.connect(
            lambda reason: window.showNormal() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        tray.show()
    window.tray_available = tray is not None

    def show_from_other_instance() -> None:
        window.showNormal()
        window.raise_()
        window.activateWindow()

    def on_new_connection() -> None:
        connection = server.nextPendingConnection()
        if connection is not None:
            connection.readyRead.connect(lambda: show_from_other_instance())
            connection.disconnected.connect(connection.deleteLater)

    server.newConnection.connect(on_new_connection)

    window.show()
    controller.start()
    code = app.exec()
    server.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
