"""目录联接（Junction）与安全路径原语 —— 所有文件结构操作的安全底线都在这。

本模块只做两类事，但每件都坚持「先验证、后动作」：

1. **创建联接**：用 ``mklink /J``（不需要管理员权限），创建后必须回读验证。
2. **删除联接**：只删联接本身，绝不递归。

为什么单独成模块：需求里的"所有删除操作前必须通过是否为 Junction /
是否位于 SSD 缓存目录内的路径白名单校验"，必须只有一个实现点，
否则迟早在某个分支里出现 ``shutil.rmtree`` 删掉目标内容的惨剧。

三条铁律：
- 删除前必须确认目标**确实是**目录联接（重解析点且 tag 为 MOUNT_POINT）；
- 删除只用 ``os.rmdir`` / ``rmdir``（**不带 /s**），永远不用 ``shutil.rmtree``；
- 任何删除目标的路径都必须先通过 ``assert_within`` 白名单校验。
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

__all__ = [
    "JunctionError",
    "PathSafetyError",
    "assert_junction_points_to",
    "assert_within",
    "create_junction",
    "is_junction",
    "is_reparse_point",
    "is_within",
    "junction_info",
    "junction_target",
    "remove_junction",
    "strip_long_prefix",
]


class JunctionError(RuntimeError):
    """创建/删除联接失败。"""


class PathSafetyError(RuntimeError):
    """路径未通过安全白名单校验——这是拒绝执行，不是警告。"""


# ------------------------------------------------------------------ 基础判定
def strip_long_prefix(path: str | os.PathLike[str]) -> str:
    """去掉 Windows 长路径前缀（``\\\\?\\`` / ``\\??\\``）。"""
    text = str(path)
    for prefix in ("\\\\?\\UNC\\", "\\??\\UNC\\"):
        if text.startswith(prefix):
            return "\\\\" + text[len(prefix):]
    for prefix in ("\\\\?\\", "\\??\\"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def is_reparse_point(path: str | os.PathLike[str]) -> bool:
    """路径是否是重解析点（联接、符号链接、挂载点都算）。"""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def is_junction(path: str | os.PathLike[str]) -> bool:
    """路径是否是目录联接（Junction / MountPoint）。

    优先使用 Python 3.12 的 ``os.path.isjunction``；3.11 及更早用重解析标签判定。
    刻意排除符号链接（SYMLINK）——本工具只创建 Junction，
    遇到符号链接应当报异常而不是当成自己的产物删除。
    """
    checker = getattr(os.path, "isjunction", None)
    if checker is not None:
        try:
            return bool(checker(Path(path)))
        except OSError:
            return False
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    tag = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
    return getattr(info, "st_reparse_tag", None) == tag


def junction_target(path: str | os.PathLike[str]) -> str:
    """返回联接指向的目标（已去掉长路径前缀）；不是联接或读取失败返回空串。"""
    try:
        return strip_long_prefix(os.readlink(path))
    except OSError:
        return ""


def junction_info(path: str | os.PathLike[str]) -> tuple[bool, str]:
    """一次返回 ``(是否联接, 目标路径)``，便于日志记录。"""
    if not is_junction(path):
        return False, ""
    return True, junction_target(path)


# ------------------------------------------------------------------ 路径白名单
def normalize_for_compare(path: str | os.PathLike[str]) -> str:
    """把路径规范化为可比较的形式（绝对路径 + 大小写不敏感 + 去尾部分隔符）。"""
    text = os.path.normcase(os.path.abspath(os.path.normpath(str(path))))
    return text.rstrip("\\/")


def is_within(child: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    """判断 child 是否等于 parent 或位于 parent 之内。"""
    c, p = normalize_for_compare(child), normalize_for_compare(parent)
    if not c or not p:
        return False
    return c == p or c.startswith(p + os.sep)


def assert_within(child: str | os.PathLike[str], parent: str | os.PathLike[str], what: str = "路径") -> None:
    """白名单校验：不在允许的根目录之内就抛异常（拒绝执行）。"""
    if not is_within(child, parent):
        raise PathSafetyError(f"{what}不在允许的范围内，已拒绝操作：{child}（允许范围：{parent}）")


def assert_junction_points_to(link: str | os.PathLike[str], expected: str | os.PathLike[str]) -> None:
    """确认联接存在且指向预期目标，否则抛异常。"""
    if not is_junction(link):
        raise JunctionError(f"路径不是目录联接：{link}")
    actual = junction_target(link)
    if normalize_for_compare(actual) != normalize_for_compare(expected):
        raise JunctionError(f"联接目标与记录不符：{link} → {actual}（预期 {expected}）")


# ------------------------------------------------------------------ 创建/删除
def _decode_console(data: bytes) -> str:
    """cmd.exe 的输出是本地代码页（中文系统上是 GBK），用宽松方式解码。"""
    for encoding in ("mbcs", "utf-8", "gbk"):
        try:
            return data.decode(encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def create_junction(link: str | os.PathLike[str], target: str | os.PathLike[str]) -> str:
    """在 link 处创建指向 target 的目录联接，返回实际目标路径。

    使用 ``mklink /J``（不需要管理员权限）。创建后**必须**回读验证：
    确认是联接、且目标与预期一致，否则抛异常让调用方回滚。
    """
    link_path = Path(os.path.abspath(str(link)))
    target_path = Path(os.path.abspath(str(target)))

    if not target_path.is_dir():
        raise JunctionError(f"联接目标不存在或不是目录：{target_path}")
    if os.path.lexists(str(link_path)):
        raise JunctionError(f"创建联接的位置已被占用：{link_path}")

    link_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link_path), str(target_path)],
        capture_output=True,
    )
    output = _decode_console(result.stdout or b"") + _decode_console(result.stderr or b"")
    if result.returncode != 0:
        raise JunctionError(f"mklink /J 失败（{result.returncode}）：{output.strip()}")

    if not is_junction(link_path):
        raise JunctionError(f"创建后验证失败，该路径不是目录联接：{link_path}")
    actual = junction_target(link_path)
    if normalize_for_compare(actual) != normalize_for_compare(target_path):
        raise JunctionError(f"创建后验证失败，联接指向 {actual}，预期 {target_path}")
    return actual


def remove_junction(link: str | os.PathLike[str]) -> None:
    """删除目录联接本身，绝不影响它指向的内容。

    只有当路径确实是目录联接时才会执行；删除后再次确认联接已消失、
    且目标仍然存在（最后一条是防呆断言，能证明"没删到目标"）。
    """
    link_path = Path(os.path.abspath(str(link)))

    if not os.path.lexists(str(link_path)):
        return
    if not is_junction(link_path):
        raise PathSafetyError(
            f"拒绝删除：该路径不是目录联接，可能是真实目录，需人工确认：{link_path}"
        )

    target = junction_target(link_path)
    # 先记录目标是否存在：恢复场景里目标可能早已被用户手工删除，
    # 那种情况下删完联接当然也"看不到目标"，不能误报为异常。
    target_existed = bool(target) and Path(target).exists()

    # os.rmdir 对 Junction 只摘除重解析点；失败时退回 rmdir（同样不带 /s）
    try:
        os.rmdir(link_path)
    except OSError:
        result = subprocess.run(["cmd", "/c", "rmdir", str(link_path)], capture_output=True)
        if result.returncode != 0:
            output = _decode_console(result.stdout or b"") + _decode_console(result.stderr or b"")
            raise JunctionError(f"删除联接失败：{link_path}：{output.strip()}") from None

    if os.path.lexists(str(link_path)):
        raise JunctionError(f"删除联接后路径仍然存在：{link_path}")
    if target_existed and not Path(target).exists():
        raise JunctionError(
            f"异常：删除联接后其目标也消失了，请立即停止操作并检查：{target}"
        )
