"""测试用的盘符挑选工具：**不写死任何盘符**，换台机器照样能跑。

为什么需要它：
  "两个盘"是本工具的核心前提（母本在机械盘、缓存在固态盘），所以端到端测试
  必须真的跨两个卷跑，不能只在同一个盘上做假。但盘符是随机器变的，
  所以这里一律**运行时探测**：

  * 母盘 = 测试沙箱所在的卷（沙箱在仓库里，跟着仓库走）；
  * 加速盘 = 另一个真实存在、能真正建目录的本地卷；
  * 挑不到就返回 None，由调用方明确说明并跳过，**绝不假装成功**。

只做"建目录"这一种真实操作来判定可写性，不依赖 os.access 的猜测，
也不会在别人的盘上留下任何探针文件。
"""

from __future__ import annotations

import os
import string
from pathlib import Path


def normalize(letter: str) -> str:
    """``"c:"`` / ``"c:\\\\"`` / ``"c"`` → ``"C"``；取不到返回空串。"""
    text = (letter or "").strip().rstrip("\\/")
    if len(text) >= 2 and text[1] == ":":
        text = text[0]
    return text.upper() if len(text) == 1 and text.isalpha() else ""


def drive_of(path: str | os.PathLike[str]) -> str:
    """取路径所在盘符（``"D"``）；取不到返回空串。"""
    drive, _tail = os.path.splitdrive(os.path.abspath(str(path)))
    return normalize(drive)


def local_volumes() -> list[str]:
    """本机所有已挂载的盘符（字母序）。"""
    try:
        letters = [normalize(item) for item in os.listdrives()]  # Python 3.12+
    except AttributeError:
        letters = [
            f"{letter}:"
            for letter in string.ascii_uppercase
            if Path(f"{letter}:\\").exists()
        ]
    return [letter for letter in letters if letter and Path(f"{letter}:\\").exists()]


def prepare_cache_root(exclude_drive: str, dir_name: str) -> Path | None:
    """在与 ``exclude_drive`` 不同的卷上建出测试缓存根目录。

    建目录这个动作本身就是可写性判据：建成功就用它，建不上就换下一个卷。
    系统盘排在最后尝试（尽量不往 C:\\ 根目录写测试数据）。
    全部失败返回 None（调用方据此跳过跨盘测试）。
    """
    skip = normalize(exclude_drive)
    system = normalize(os.environ.get("SystemDrive", ""))
    letters = [letter for letter in local_volumes() if letter != skip]
    letters.sort(key=lambda letter: (letter == system, letter))
    for letter in letters:
        root = Path(f"{letter}:\\") / dir_name
        try:
            root.mkdir(parents=True, exist_ok=False)
        except OSError:
            continue
        return root
    return None
