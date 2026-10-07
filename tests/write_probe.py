"""诊断用最小程序：打包成 exe 后测试"能不能往这些目录写文件"。

不是测试套件的一部分，只在排查"打包后写入被拒"时用。
构建（在临时目录里，跑完即删）：
    pyinstaller --noconfirm --onedir --console --distpath <tmp>\\dist --workpath <tmp>\\build tests\\write_probe.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

print(f"frozen = {getattr(sys, 'frozen', False)}")
print(f"exe    = {sys.executable}")
print(f"cwd    = {os.getcwd()}")

targets = [
    Path(tempfile.gettempdir()) / "sb_probe_temp.log",
    Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "sb_probe_localappdata.log",
    Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "SteamBoost" / "sb_probe.log",
    Path(os.environ.get("APPDATA", tempfile.gettempdir())) / "sb_probe_roaming.log",
    Path(os.environ.get("USERPROFILE", tempfile.gettempdir())) / "sb_probe_profile.log",
    Path(os.getcwd()) / "sb_probe_cwd.log",
]

for target in targets:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("probe\n", encoding="utf-8")
        print(f"[OK]   {target}")
    except OSError as exc:
        print(f"[FAIL] {target}  → {exc}")
