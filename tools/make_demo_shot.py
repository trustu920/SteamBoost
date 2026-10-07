"""生成用于文档 / 仓库展示的界面截图（**全部使用虚构数据**）。

为什么不用 ``tests/screenshot_gui.py``
--------------------------------------
那份脚本按**真实扫描结果**渲染，截图里的盘符、库路径、游戏名与封面都属于
"这台电脑"的信息，贴到公开仓库等于把自己的机器和游戏库一起公开。

所以这里手工构造一份完全虚构的数据：

* 两个不存在的盘符 ``X:``（母盘）与 ``Y:``（加速盘），容量与占用是编的；
* 12 款**不存在的**游戏（名字、安装目录、appid 都是编的）；
* 封面用程序自带的占位图（渐变 + 名字），不联网、不下载任何真实封面；
* 卷容量查询、系统盘列表、FastCopy 查找、封面目录全部在运行期被替换掉，
  真实环境里的路径一个都不会出现在图上。

运行： ``python tools/make_demo_shot.py [输出目录]``（默认写到 ``docs/``）
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 离屏渲染需要字体目录，从系统环境变量推导，不写死任何盘符
_system_root = os.environ.get("SystemRoot")
if _system_root:
    os.environ.setdefault("QT_QPA_FONTDIR", str(Path(_system_root) / "Fonts"))

from PySide6.QtCore import QItemSelectionModel  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from config import Config  # noqa: E402
from steam_scanner import (  # noqa: E402
    ST_ACCELERATED,
    ST_ON_HDD,
    GameRecord,
    ScanReport,
    SteamLibrary,
)
from ui import covers, main_window, settings_dialog, theme  # noqa: E402
from ui.confirm_dialog import ConfirmExtrasDialog  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.settings_dialog import SettingsDialog  # noqa: E402

GB = 1024 ** 3
HOUR = 3600.0
DAY = 24 * HOUR

#: 虚构的盘：盘符、总容量、可用容量（全是编的，用来画顶栏那两条占用条）
FAKE_VOLUMES = {
    "X": {"total": int(3 * 1024 * GB), "free": int(640 * GB)},   # 母盘：大容量机械盘
    "Y": {"total": int(1 * 1024 * GB), "free": int(302 * GB)},   # 加速盘：固态盘
}
#: 虚构的 Steam 库
LIBRARIES = (("X", "SteamLibrary"), ("Y", "SteamLibrary"))
#: 虚构的游戏：(名字, 安装目录名, appid, 大小 GB, 状态, 最近游玩距今天数)
GAMES = (
    ("Neon Circuit", "NeonCircuit", "905101", 68.4, ST_ACCELERATED, 1),
    ("Steel Horizon IV", "SteelHorizon4", "905102", 92.1, ST_ACCELERATED, 3),
    ("Ironwood Valley", "IronwoodValley", "905103", 120.5, ST_ACCELERATED, 2),
    ("Nebula Drifter", "NebulaDrifter", "905104", 58.2, ST_ACCELERATED, 9),
    ("Pixel Kingdoms", "PixelKingdoms", "905105", 4.8, ST_ON_HDD, 6),
    ("Glacier Run", "GlacierRun", "905106", 12.6, ST_ON_HDD, 5),
    ("Crimson Forge", "CrimsonForge", "905107", 54.7, ST_ON_HDD, 12),
    ("Starfall Tactics", "StarfallTactics", "905108", 44.3, ST_ON_HDD, 25),
    ("Silent Harbor", "SilentHarbor", "905109", 31.2, ST_ON_HDD, 40),
    ("Ashen Crown", "AshenCrown", "905110", 76.9, ST_ON_HDD, 60),
    ("Verdant Skies", "VerdantSkies", "905111", 88.0, ST_ON_HDD, 15),
    ("Midnight Cartographer", "MidnightCartographer", "905112", 2.4, ST_ON_HDD, 100),
)
#: 虚构的"回写时母盘多出来的文件"（演示隔离区确认框）
FAKE_EXTRAS = (
    "X:\\SteamLibrary\\steamapps\\common\\NeonCircuit\\bin\\old_patch_01.pak",
    "X:\\SteamLibrary\\steamapps\\common\\NeonCircuit\\bin\\old_patch_02.pak",
    "X:\\SteamLibrary\\steamapps\\common\\NeonCircuit\\data\\deprecated\\locale_ru.dat",
    "X:\\SteamLibrary\\steamapps\\common\\NeonCircuit\\logs\\client_2024_11_03.log",
    "X:\\SteamLibrary\\steamapps\\common\\NeonCircuit\\logs\\client_2024_11_04.log",
)


def library_path(drive: str, name: str) -> str:
    return f"{drive}:\\{name}"


def build_report(now: float) -> ScanReport:
    """构造一份完全虚构的扫描结果。"""
    libraries: list[SteamLibrary] = []
    games: list[GameRecord] = []
    for drive, name in LIBRARIES:
        libraries.append(
            SteamLibrary(
                path=library_path(drive, name),
                label=f"{name} ({drive}:)",
                exists=True,
                source="libraryfolders.vdf",
            )
        )
    for title, installdir, appid, size_gb, status, played_days in GAMES:
        mother = library_path("X", "SteamLibrary")
        game_path = f"{mother}\\steamapps\\common\\{installdir}"
        cache_copy = f"Y:\\SteamBoostCache\\{appid}_{installdir}"
        accelerated = status == ST_ACCELERATED
        games.append(
            GameRecord(
                appid=appid,
                name=title,
                installdir=installdir,
                library_path=mother,
                size_on_disk=int(size_gb * GB),
                last_played=int(now - played_days * DAY),
                acf_path=f"{mother}\\steamapps\\appmanifest_{appid}.acf",
                game_path=game_path,
                hdd_backup_path=f"{mother}\\steamapps\\common\\.hdd_cache\\{installdir}",
                cache_copy_path=cache_copy,
                status=status,
                junction_target=cache_copy if accelerated else "",
                cache_copy_exists=accelerated,
                hdd_backup_exists=accelerated,
            )
        )
    return ScanReport(
        steam_root="X:\\Steam",
        libraries=libraries,
        games=games,
        anomalies=[],
        volumes=[],
        cache_dir="Y:\\SteamBoostCache",
        cache_dir_configured=True,
        quarantine_items=[],
    )


def build_config(sandbox: Path) -> Config:
    """虚构配置：两个盘、日志与封面缓存都落在临时沙箱里。"""
    return Config(
        mother_drive="X",
        cache_drive="Y",
        cache_dir="Y:\\SteamBoostCache",
        copy_engine="fastcopy",
        fastcopy_path="",
        robocopy_threads=8,
        fastcopy_verify=False,
        free_space_min_percent=10.0,
        free_space_min_gb=20.0,
        # 设置页会把日志目录**显示出来**，所以这里也必须给一个虚构路径
        log_dir="Y:\\SteamBoost\\logs",
        cover_cache_dir=str(sandbox / "covers"),
        minimize_to_tray=True,
    )


def install_fakes(sandbox: Path) -> None:
    """把所有"会读到真实环境"的入口换成虚构实现。"""
    covers_dir = sandbox / "covers"
    covers_dir.mkdir(parents=True, exist_ok=True)
    covers.cover_dir = lambda: covers_dir  # type: ignore[assignment]

    def fake_total(drive: str) -> int:
        return FAKE_VOLUMES.get((drive or "").upper().rstrip(":"), {}).get("total", 0)

    def fake_free(drive: str) -> int:
        return FAKE_VOLUMES.get((drive or "").upper().rstrip(":"), {}).get("free", 0)

    main_window.volume_total_bytes = fake_total  # type: ignore[assignment]
    main_window.volume_free_bytes = fake_free  # type: ignore[assignment]

    # 设置页的两个下拉来自 list_volumes()，FastCopy 一行来自 find_fastcopy()
    settings_dialog.list_volumes = lambda: [  # type: ignore[assignment]
        {
            "drive": drive,
            "total": values["total"],
            "free": values["free"],
        }
        for drive, values in FAKE_VOLUMES.items()
    ]
    settings_dialog.find_fastcopy = lambda explicit="": Path("C:/Program Files/FastCopy/fcp.exe")  # type: ignore[assignment]
    # 设置页会显示日志目录并统计其占用；虚构盘并不存在，而 logger 会顺手 mkdir，
    # 所以这里把用量统计换成常量（图示里的路径只是排版用的占位）。
    settings_dialog.log_usage = lambda cfg=None: (0, 0)  # type: ignore[assignment]


def pump(app: QApplication, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def shoot(widget, app: QApplication, target: Path, size: tuple[int, int] | None = None) -> None:
    """把控件抓成 PNG。

    对话框的尺寸用它的 ``sizeHint``：离屏渲染时 ``adjustSize()`` 会给出比
    ``sizeHint`` 小的高度（724 vs 775），布局被压扁后控件文字会被裁掉一半。
    """
    if size is None:
        hint = widget.sizeHint()
        widget.resize(
            max(widget.minimumWidth(), hint.width()),
            max(widget.minimumHeight(), hint.height()),
        )
    else:
        widget.resize(*size)
    widget.show()
    pump(app, 0.6)
    saved = widget.grab().save(str(target))
    print(f"  {'已保存' if saved else '保存失败'}：{target.name}"
          f"（{widget.width()}×{widget.height()}，{target.stat().st_size // 1024} KB）")
    widget.hide()


def main() -> int:
    out_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else (ROOT / "docs")
    out_dir.mkdir(parents=True, exist_ok=True)

    sandbox = Path(tempfile.mkdtemp(prefix="steamboot_demo_"))
    app = QApplication(sys.argv)
    theme.apply_theme(app)

    try:
        install_fakes(sandbox)
        cfg = build_config(sandbox)
        now = time.time()

        print("渲染主界面…")
        window = MainWindow(cfg, allow_cover_download=False)
        window.set_engine_text("FastCopy")
        window.load_report(build_report(now))
        # 选中两张卡，让"已选中 2 项 / 批量操作"这一行也有内容
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        for row in (0, 1):
            window.view.selectionModel().select(window.model.index(row, 0), flags)
        shoot(window, app, out_dir / "screenshot-main.png", (1180, 830))

        print("渲染设置页…")
        dialog = SettingsDialog(cfg, libraries=[lib.path for lib in build_report(now).libraries])
        shoot(dialog, app, out_dir / "screenshot-settings.png")

        print("渲染回写确认（隔离区）对话框…")
        confirm = ConfirmExtrasDialog("Neon Circuit", list(FAKE_EXTRAS), int(6.8 * GB))
        shoot(confirm, app, out_dir / "screenshot-writeback-confirm.png")

        print("完成。")
        return 0
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
