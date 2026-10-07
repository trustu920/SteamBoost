"""界面离屏渲染截图（视觉验证用）。

做法：把 Qt 切到 ``offscreen`` 平台插件，用**真实扫描结果**构建主窗口，
等封面下载完成后把窗口抓成 PNG。这样在没有显示器的环境里也能检查排版。

运行： python tests/screenshot_gui.py [输出路径]
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from drive_picker import drive_of  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

from config import Config, list_volumes  # noqa: E402
from steam_scanner import scan  # noqa: E402
from ui import theme  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402


def pump(app: QApplication, seconds: float) -> None:
    """跑一段时间的事件循环（不阻塞界面）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (Path(__file__).resolve().parent / "_gui_shot.png")

    app = QApplication(sys.argv)
    theme.apply_theme(app)

    sandbox = Path(__file__).resolve().parent / "_shot_sandbox"
    cfg = Config.load()
    # 日志目录指向沙箱：设置页会显示真实日志路径，指向沙箱可避免截图带上本机用户目录
    cfg.log_dir = str(sandbox / "logs")
    if not cfg.mother_drive or not cfg.cache_drive:
        # 配置还没建立时，从真实 Steam 库里推一个（不写死任何盘符）
        probe = scan(Config())
        libraries = [lib for lib in probe.libraries if lib.exists]
        if libraries:
            cfg.mother_drive = drive_of(libraries[0].path)
            cfg.cache_drive = next(
                (vol["drive"] for vol in list_volumes() if vol["drive"] != cfg.mother_drive),
                cfg.mother_drive,
            )
            cfg.cache_dir = f"{cfg.cache_drive}:\\SteamBoostCache"

    print("扫描中…")
    report = scan(cfg)
    print(f"  游戏 {len(report.games)} 款，库 {len(report.libraries)} 个，异常 {len(report.anomalies)} 条")

    window = MainWindow(cfg)
    window.resize(1180, 830)
    window.load_report(report)
    window.show()
    pump(app, 0.5)

    # 等封面下载（最多 40 秒），让截图里出现真实封面而不是占位图
    deadline = time.time() + 40
    while time.time() < deadline:
        app.processEvents()
        pending = [g.appid for g in report.games if not window.cover_cache.is_fully_cached(g.appid)]
        if not pending:
            break
        time.sleep(0.25)
    pump(app, 1.0)

    cached = sum(1 for g in report.games if window.cover_cache.is_fully_cached(g.appid))
    print(f"  本地封面 {cached}/{len(report.games)} 张")

    window.grab().save(str(out))
    print(f"已保存：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
