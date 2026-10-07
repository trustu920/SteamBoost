# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（**文件夹版 onedir**）。

构建：
    python tools\\make_icon.py
    pyinstaller --clean --noconfirm SteamBoost.spec

产物：``dist/SteamBoost/SteamBoost.exe``（同目录下还有 Qt 的 DLL 与插件，整包一起拷走即可用）。

为什么不用单文件（onefile）？
----------------------------
单文件 exe 每次启动都要先把自己解压到一个临时目录并加固其权限。
在部分系统上（例如本机）这一步会被系统挡住，表现为启动即弹
``Could not create temporary directory!`` —— 实测**最小 hello-world 程序同样失败**，
换 PyInstaller 版本、换临时目录、指定 ``--runtime-tmpdir`` 都无效，属于环境限制。
文件夹版不做任何解压，启动更快，也避免了这个问题。

不捆绑 FastCopy：程序运行时自动探测本机已安装的 fcp.exe，找不到就回退到 robocopy。
若确实想把它放进程序目录，把 FastCopy 整个目录复制到 ``tools/FastCopy/``，
再把下面的 ``BUNDLE_FASTCOPY`` 改成 True 重新构建。
"""

from pathlib import Path

BUNDLE_FASTCOPY = False

root = Path(SPECPATH)
hiddenimports = [
    "psutil",
    "ui.theme", "ui.covers", "ui.game_model", "ui.main_window",
    "ui.progress_panel", "ui.workers", "ui.confirm_dialog",
    "ui.settings_dialog", "ui.quarantine_dialog", "ui.controller",
    "ui.repair_dialog", "ui.space_warning", "ui.icons",
]

binaries = []
if BUNDLE_FASTCOPY:
    fastcopy_dir = root / "tools" / "FastCopy"
    if fastcopy_dir.is_dir():
        for item in fastcopy_dir.iterdir():
            if item.is_file():
                binaries.append((str(item), "tools/FastCopy"))

a = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 用不到的大件，排除后体积明显变小（PySide6 只保留 QtCore/QtGui/QtWidgets/QtNetwork）
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
        "PySide6.QtMultimedia", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtCharts",
        "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtOpenGL", "PySide6.QtPdf",
        "tkinter", "matplotlib", "numpy", "pandas", "PIL", "PyInstaller",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # 文件夹版：动态库放到 COLLECT 里，不打进 exe
    name="SteamBoost",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                  # GUI 程序：不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "assets" / "steamboot.ico") if (root / "assets" / "steamboot.ico").is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SteamBoost",
)
