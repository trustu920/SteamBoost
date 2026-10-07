"""复制引擎：FastCopy（首选）与 robocopy（回退）。

为什么这样设计
--------------
* **FastCopy 首选**（本机决策：个人自用，符合其家庭免费授权）：
  异步 I/O、大小文件分别处理，小文件场景明显快于 robocopy；
  ``/cmd=diff`` 就是差异复制（断点续拷语义），``/cmd=sync`` 就是镜像回写。
* **robocopy 回退**：Windows 自带，任何机器上都能跑，保证程序不会因为
  找不到 FastCopy 而不可用。

实测得到的硬约束（见 tests/probe_fastcopy.py）
--------------------------------------------
1. ``/to=`` **必须是最后一个参数**，否则报 "Too few/many argument" 并弹错框。
2. FastCopy 的 stdout/stderr 为空，**没有实时进度流**，结果只能读它写的日志；
   因此进度用"目标目录已落盘字节数"轮询得到。
3. 它会读取同目录 FastCopy2.ini，环境配置会**悄悄影响**行为
   （例如本机 ini 里 verify="1" 默认就开了校验），所以引擎必须显式传参。
4. 退出码 0 成功、-1 失败；日志里的 ``Result : (ErrFiles : N / ErrDirs : N)``
   是最权威的成功判据，两者都要看。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from config import Config
from junction_utils import is_within
from logger import setup_logger

log = setup_logger("steamboot.copy")

# ------------------------------------------------------------------ 常量
#: FastCopy 的可执行文件名：fcp.exe 是命令行版（脚本友好），FastCopy.exe 是界面版
FASTCOPY_NAMES = ("fcp.exe", "FastCopy.exe")
#: 安装目录名（与 %ProgramFiles% 等**系统环境变量**组合成候选路径，不写死任何绝对路径）
FASTCOPY_DIR_NAME = "FastCopy"
#: 注册表卸载信息的位置（只读；用来发现用户实际把它装在哪）
UNINSTALL_LOCATIONS = (
    ("HKEY_CURRENT_USER", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
)
#: FastCopy 自己的注册表键（部分安装方式会在这里记录安装目录）
FASTCOPY_REGISTRY_KEYS = (
    ("HKEY_CURRENT_USER", r"SOFTWARE\FastCopy"),
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\FastCopy"),
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\FastCopy"),
)
#: 注册表里可能记录安装目录的值名（空串 = 默认值）
INSTALL_PATH_VALUES = ("InstallLocation", "InstallDir", "DisplayIcon", "UninstallString", "Path", "ExePath", "")
#: 进度轮询间隔（秒）
POLL_INTERVAL = 1.0
#: 目录条目超过这个数量就放弃轮询（避免进度扫描本身拖慢复制）
POLL_ENTRY_LIMIT = 300_000

MODE_DIFF = "diff"        # 只复制差异，不删除目标多余文件
MODE_MIRROR = "mirror"    # 差异复制 + 删除目标多余文件（镜像）


class CopyCancelled(RuntimeError):
    """用户取消了复制。"""


class CopyFailed(RuntimeError):
    """复制失败。``detail`` 可直接展示给用户。"""

    def __init__(self, message: str, detail: str = "", exit_code: int | None = None) -> None:
        super().__init__(message)
        self.detail = detail
        self.exit_code = exit_code


# ------------------------------------------------------------------ 数据结构
@dataclass
class TreeStats:
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    entries: dict[str, tuple[int, int]] = field(default_factory=dict)  # rel → (size, mtime_ns)

    @property
    def total(self) -> int:
        return self.files + self.dirs


@dataclass
class ProgressState:
    """一次进度回调携带的全部信息，GUI 直接绑定即可。"""

    percent: float = 0.0
    bytes_done: int = 0
    bytes_total: int = 0
    files_done: int = 0
    files_total: int = 0
    speed_bps: float = 0.0
    eta_seconds: float | None = None
    elapsed: float = 0.0

    @property
    def speed_text(self) -> str:
        return f"{self.speed_bps / 1048576:.1f} MB/s" if self.speed_bps > 0 else "—"

    @property
    def eta_text(self) -> str:
        if self.eta_seconds is None or self.eta_seconds <= 0:
            return "—"
        seconds = int(self.eta_seconds)
        if seconds < 60:
            return f"{seconds} 秒"
        if seconds < 3600:
            return f"{seconds // 60} 分 {seconds % 60} 秒"
        return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分"


@dataclass
class CopyResult:
    ok: bool = False
    engine: str = ""
    mode: str = ""
    exit_code: int | None = None
    files_copied: int = 0
    dirs_copied: int = 0
    bytes_copied: int = 0
    files_skipped: int = 0
    files_deleted: int = 0
    bytes_deleted: int = 0
    error_files: int = 0
    elapsed: float = 0.0
    log_path: str = ""
    detail: str = ""


@dataclass
class VerifyResult:
    ok: bool = False
    files_src: int = 0
    files_dst: int = 0
    bytes_src: int = 0
    bytes_dst: int = 0
    missing: list[str] = field(default_factory=list)      # 目标缺少
    extra: list[str] = field(default_factory=list)        # 目标多出
    size_mismatch: list[str] = field(default_factory=list)
    mtime_mismatch: list[str] = field(default_factory=list)
    detail: str = ""

    def summary(self) -> str:
        parts = [
            f"文件数 {self.files_src} → {self.files_dst}",
            f"总字节 {self.bytes_src} → {self.bytes_dst}",
        ]
        if self.missing:
            parts.append(f"缺失 {len(self.missing)}")
        if self.extra:
            parts.append(f"多出 {len(self.extra)}")
        if self.size_mismatch:
            parts.append(f"大小不符 {len(self.size_mismatch)}")
        if self.mtime_mismatch:
            parts.append(f"时间戳不符 {len(self.mtime_mismatch)}")
        return "，".join(parts)


# ------------------------------------------------------------------ 基础工具
def _winreg_module():
    """惰性导入 ``winreg``（非 Windows 或导入失败时返回 None）。"""
    if os.name != "nt":
        return None
    try:
        import winreg  # noqa: PLC0415
    except ImportError:  # pragma: no cover - 正常 Windows 上不会发生
        return None
    return winreg


def _registry_value(key, name: str) -> str:
    """读一个注册表值，读不到就返回空串（不抛异常）。"""
    winreg = _winreg_module()
    if winreg is None:
        return ""
    try:
        value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return "" if value is None else str(value)


def path_from_registry_value(text: str) -> Path | None:
    """从注册表值里解析出安装目录。

    真实值的三种典型写法都要能吃下（``<PF>`` 代表 ``%ProgramFiles%`` 这类环境变量的实际取值）：

    * ``InstallLocation`` = ``<PF>\\FastCopy``
    * ``DisplayIcon``     = ``<PF>\\FastCopy\\FastCopy.exe,0``
    * ``UninstallString`` = ``"<PF>\\FastCopy\\unins000.exe" /SILENT``

    规则：先剥掉引号与其后的参数，再判断它指向文件还是目录——指向文件就取所在目录。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith('"'):
        end = raw.find('"', 1)
        raw = raw[1:end] if end > 0 else raw[1:]
    elif re.match(r"^[A-Za-z]:[\\/]", raw):
        # 没有引号时，参数用空格分隔；逗号后面则是图标索引（如 ,0）
        raw = raw.split(" /", 1)[0].strip()
        raw = raw.split(" -", 1)[0].strip()
    raw = raw.rstrip().rstrip(",")
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.suffix:
        # 指向的是可执行文件（甚至是 "FastCopy.exe,0" 这种）→ 取它所在的目录
        candidate = candidate.parent
    if not candidate.is_absolute():
        # 注册表里出现相对路径说明这条记录不能用，绝不猜
        return None
    return candidate


def registry_fastcopy_dirs() -> list[Path]:
    """从注册表里找出 FastCopy 的安装目录（找不到就返回空列表，绝不抛异常）。"""
    winreg = _winreg_module()
    if winreg is None:
        return []
    found: list[Path] = []

    def remember(value: str) -> None:
        path = path_from_registry_value(value)
        if path is not None and path not in found:
            found.append(path)

    for hive_name, subkey in UNINSTALL_LOCATIONS:
        hive = getattr(winreg, hive_name, None)
        if hive is None:
            continue
        try:
            with winreg.OpenKey(hive, subkey) as root:
                count = winreg.QueryInfoKey(root)[0]
                for index in range(count):
                    try:
                        child = winreg.EnumKey(root, index)
                        with winreg.OpenKey(root, child) as item:
                            if "fastcopy" not in _registry_value(item, "DisplayName").lower():
                                continue
                            for value_name in INSTALL_PATH_VALUES:
                                remember(_registry_value(item, value_name))
                    except OSError:
                        continue
        except OSError:
            continue

    for hive_name, subkey in FASTCOPY_REGISTRY_KEYS:
        hive = getattr(winreg, hive_name, None)
        if hive is None:
            continue
        try:
            with winreg.OpenKey(hive, subkey) as key:
                for value_name in INSTALL_PATH_VALUES:
                    remember(_registry_value(key, value_name))
        except OSError:
            continue
    return found


def fastcopy_candidate_dirs() -> list[Path]:
    """FastCopy 的候选安装目录（不含显式配置，也不含 PATH）。

    顺序：注册表记录的真实安装位置 → 系统环境变量下的常见安装位置 → 程序自带目录。
    **全部由系统自身信息推导，不写死任何绝对路径**，换台机器照样能用。
    """
    dirs: list[Path] = list(registry_fastcopy_dirs())
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(variable)
        if base:
            dirs.append(Path(base) / FASTCOPY_DIR_NAME)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        # 不少安装器把程序放进 %LOCALAPPDATA%\Programs\<名字>
        dirs.append(Path(local) / "Programs" / FASTCOPY_DIR_NAME)
    here = Path(__file__).resolve().parent
    dirs.extend((here / "tools" / FASTCOPY_DIR_NAME, here / FASTCOPY_DIR_NAME, here))

    unique: list[Path] = []
    for path in dirs:
        if path not in unique:
            unique.append(path)
    return unique


def find_fastcopy(explicit: str = "") -> Path | None:
    """定位 FastCopy 的 CUI 版本（fcp.exe 优先，脚本友好）。

    查找顺序：显式配置 → 注册表记录的位置 → 系统环境变量下的常见位置
    → 程序自带目录 → 系统 PATH。找不到返回 None（调用方负责回退 robocopy）。
    """
    candidates: list[Path] = []
    if explicit.strip():
        candidates.append(Path(explicit))
    candidates.extend(
        directory / name for directory in fastcopy_candidate_dirs() for name in FASTCOPY_NAMES
    )
    for name in FASTCOPY_NAMES:
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def fastcopy_available(cfg: Config | None = None) -> bool:
    """当前配置下 FastCopy 是否真的可用（界面用它给出"会回退 robocopy"的提示）。"""
    config = cfg or Config.load()
    return find_fastcopy(config.fastcopy_path) is not None


def assert_disjoint(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """断言源与目标互不包含——这是防"自引用复制"的硬性检查。

    镜像回写时若把目标指到源里面（或反过来），复制会边写边读自己，
    轻则重复嵌套，重则把母本目录结构毁掉，所以执行前必须拦住。
    """
    if is_within(src, dst) or is_within(dst, src):
        raise CopyFailed(f"源与目标不能相同或互相包含：{src} ↔ {dst}")
    for path in (src, dst):
        text = os.path.abspath(str(path))
        if os.path.splitdrive(text)[0] and os.path.normpath(text) == os.path.normpath(os.path.splitdrive(text)[0] + os.sep):
            raise CopyFailed(f"拒绝把盘根目录当作源或目标：{text}")


def normalize_fastcopy_dest(dst: str | os.PathLike[str]) -> str:
    """规范化 FastCopy 的 ``/to=`` 参数：**必须去掉尾部反斜杠**。

    实测语义（官方文档亦明确）：

    * 以 ``\\`` 结尾 → 把**源目录本身**复制进去（dst\\src目录名\\内容）
    * 不以 ``\\`` 结尾 → 把**源目录的内容**复制进去（dst\\内容）

    本工具要的是后者。若把前者用错，HDD 母本会被多嵌套一层，
    对"母本永远保持完整可用"是不可接受的破坏。
    """
    text = str(dst)
    if text.endswith(("\\", "/")):
        trimmed = text.rstrip("\\/")
        # 盘根的尾部反斜杠不能删（FastCopy 文档要求），而这种目标我们本来就不允许
        if len(trimmed) == 2 and trimmed.endswith(":"):
            raise CopyFailed(f"拒绝把盘根作为复制目标：{text}")
        return trimmed
    return text


def scan_tree(root: str | os.PathLike[str], *, with_entries: bool = False, stop_event=None) -> TreeStats:
    """统计一棵树的文件数/目录数/总字节（可选记录每个文件的相对路径与大小）。

    遇到重解析点（Junction）**不跟随**，只当作一个目录条目，
    避免扫描顺着联接冲进另一个盘。
    """
    stats = TreeStats()
    base = Path(root)
    if not base.is_dir():
        return stats
    stack: list[tuple[Path, str]] = [(base, "")]
    while stack:
        if stop_event is not None and stop_event.is_set():
            break
        current, prefix = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            rel = f"{prefix}{entry.name}"
            try:
                if entry.is_dir(follow_symlinks=False):
                    stats.dirs += 1
                    stack.append((Path(entry.path), rel + os.sep))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            stats.files += 1
            stats.bytes += info.st_size
            if with_entries:
                stats.entries[rel] = (info.st_size, info.st_mtime_ns)
    if with_entries:
        stats.entries[""] = (0, int(base.stat().st_mtime_ns))
    return stats


def plan_extras(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> tuple[list[str], int]:
    """列出"源里没有、镜像时会被删除"的目标条目。

    与 FastCopy ``Sync(Size/Date)`` 的删除判据保持一致：**目标里的相对路径
    在源里是否存在**（不看大小/时间）。因此这份清单就是用户确认后真正会被删的东西。
    返回 ``(相对路径列表, 总字节)``，目录排在文件之后（先删文件再删空目录）。
    """
    src_root, dst_root = Path(src), Path(dst)
    if not dst_root.is_dir():
        return [], 0
    src_names: set[str] = set()
    if src_root.is_dir():
        stack = [src_root]
        while stack:
            current = stack.pop()
            try:
                for entry in os.scandir(current):
                    rel = os.path.relpath(entry.path, src_root)
                    src_names.add(os.path.normcase(rel))
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
            except OSError:
                continue

    files_out: list[str] = []
    dirs_out: list[str] = []
    total_bytes = 0
    stack = [dst_root]
    while stack:
        current = stack.pop()
        try:
            for entry in os.scandir(current):
                rel = os.path.relpath(entry.path, dst_root)
                if os.path.normcase(rel) in src_names:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    continue
                if entry.is_dir(follow_symlinks=False):
                    dirs_out.append(rel)
                    stack.append(Path(entry.path))
                else:
                    files_out.append(rel)
                    try:
                        total_bytes += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            continue
    return sorted(files_out) + sorted(dirs_out), total_bytes


def verify_trees(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    *,
    compare_mtime: bool = True,
    allow_extra: bool = True,
    mtime_tolerance_ns: int = 0,
    max_report: int = 50,
    stop_event=None,
) -> VerifyResult:
    """校验强度档位 ① ：文件数 + 总字节 + 逐文件大小/时间戳。

    ``allow_extra`` 决定"目标多出来的文件"算不算失败：

    * ``True``（差异复制后用）：只要求"源里的文件都完整到达"；
    * ``False``（**镜像回写后用**）：要求两侧完全一致，多一个文件也算失败。

    时间戳默认**精确比较**（``mtime_tolerance_ns=0``）。理由：本工具两侧都是 NTFS
    （100ns 精度），FastCopy 的比对容差也是 0ms，放宽容差会让"长度相同的静默损坏"
    完全查不出来——而这是档位 ① 唯一还能抓住它的手段。

    内存友好：不把整棵树的两个清单都存下来，只保留源清单，
    再流式遍历目标做比对（百万文件级别也不会吃掉几个 GB）。
    """
    src_stats = scan_tree(src, with_entries=True, stop_event=stop_event)
    dst_stats = scan_tree(dst, with_entries=True, stop_event=stop_event)
    result = VerifyResult(
        files_src=src_stats.files,
        files_dst=dst_stats.files,
        bytes_src=src_stats.bytes,
        bytes_dst=dst_stats.bytes,
    )

    for rel, (size, mtime) in src_stats.entries.items():
        if rel == "":
            continue
        found = dst_stats.entries.get(rel)
        if found is None:
            if len(result.missing) < max_report:
                result.missing.append(rel)
            continue
        if found[0] != size:
            if len(result.size_mismatch) < max_report:
                result.size_mismatch.append(f"{rel}（{size} → {found[0]}）")
        elif compare_mtime and abs(found[1] - mtime) > mtime_tolerance_ns:
            if len(result.mtime_mismatch) < max_report:
                result.mtime_mismatch.append(rel)

    for rel in dst_stats.entries:
        if rel and rel not in src_stats.entries:
            if len(result.extra) < max_report:
                result.extra.append(rel)

    same_counts = src_stats.files == dst_stats.files and src_stats.bytes == dst_stats.bytes
    ok = not result.missing and not result.size_mismatch
    if not allow_extra:
        ok = ok and same_counts and not result.extra
    if compare_mtime and result.mtime_mismatch:
        ok = False
    result.ok = ok
    result.detail = result.summary()
    return result


# ------------------------------------------------------------------ 引擎基类
class BaseEngine:
    """复制引擎接口。所有引擎都必须是"可取消、可报告进度、可回读结果"的。"""

    name = "base"
    #: 界面上显示的短名（状态栏用）
    label = "未知引擎"

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config.load()

    def copy_tree(
        self,
        src: str | os.PathLike[str],
        dst: str | os.PathLike[str],
        *,
        mode: str = MODE_DIFF,
        verify: bool = False,
        on_progress: Callable[[ProgressState], None] | None = None,
        cancel_event: threading.Event | None = None,
        total: TreeStats | None = None,
    ) -> CopyResult:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


# ------------------------------------------------------------------ FastCopy
_FC_RESULT_RE = re.compile(r"Result\s*:\s*\(ErrFiles\s*:\s*(\d+)\s*/\s*ErrDirs\s*:\s*(\d+)\)")
_FC_NUM_RE = re.compile(r"([\d,\.]+)")


def _fc_int(text: str) -> int:
    match = _FC_NUM_RE.search(text or "")
    if not match:
        return 0
    return int(match.group(1).replace(",", "").split(".")[0] or 0)


def parse_fastcopy_log(text: str) -> dict[str, int | str]:
    """解析 FastCopy 结果日志。

    实测格式（UTF-8）::

        <Command> 差异（大小/日期） (with Verify)
        TotalRead  = 12.3 MiB
        TotalFiles = 1,234 (56)
        SkipFiles  = 10 (0)
        TotalDel   = 0.0 MiB
        DelFiles   = 0 (0)
        Result : (ErrFiles : 0 / ErrDirs : 0)
    """
    info: dict[str, int | str] = {}
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("<Command>"):
            info["command"] = stripped[len("<Command>"):].strip()
            continue
        match = _FC_RESULT_RE.search(stripped)
        if match:
            info["err_files"] = int(match.group(1))
            info["err_dirs"] = int(match.group(2))
            info["result_line"] = stripped
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in {"TotalFiles", "SkipFiles", "DelFiles"}:
            info[key] = _fc_int(value)
        elif key in {"TotalRead", "TotalWrite", "TotalSkip", "TotalDel"}:
            info[key] = value.strip()
        elif key in {"TotalTime", "TransRate", "FileRate"}:
            info[key] = value.strip()
    return info


class FastCopyEngine(BaseEngine):
    """FastCopy（fcp.exe）引擎。

    进度来源：轮询目标目录已落盘字节数（FastCopy 不输出实时进度）。
    结果来源：它写的日志文件 + 进程退出码，两者都检查。
    """

    name = "fastcopy"
    label = "FastCopy"

    def __init__(self, cfg: Config | None = None, executable: str | Path | None = None) -> None:
        super().__init__(cfg)
        self.executable = Path(executable) if executable else find_fastcopy()
        if self.executable is None or not Path(self.executable).is_file():
            raise CopyFailed("未找到 FastCopy（fcp.exe），请安装或改用 robocopy 引擎")

    def describe(self) -> str:
        return f"FastCopy（{self.executable}）"

    def build_args(
        self,
        src: str | os.PathLike[str],
        dst: str | os.PathLike[str],
        *,
        mode: str,
        verify: bool,
        log_path: str | os.PathLike[str],
    ) -> list[str]:
        """构造命令行。注意 ``/to=`` 必须排在最后（实测约束）。"""
        cmd = "sync" if mode == MODE_MIRROR else "diff"
        return [
            str(self.executable),
            f"/cmd={cmd}",
            "/no_ui",                 # 后台运行：不弹任何对话框，否则隐藏窗口时会假死
            "/error_stop=FALSE",      # 出错继续，由我们读日志判定，避免半途弹框
            f"/verify={'TRUE' if verify else 'FALSE'}",  # 必须显式指定，否则被 ini 里的设置影响
            "/log",
            f"/logfile={log_path}",
            "/speed=full",
            "/estimate=FALSE",
            "/balloon=FALSE",
            str(src),
            f"/to={normalize_fastcopy_dest(dst)}",  # 必须是最后一个参数，且不能带尾反斜杠
        ]

    def copy_tree(
        self,
        src: str | os.PathLike[str],
        dst: str | os.PathLike[str],
        *,
        mode: str = MODE_DIFF,
        verify: bool = False,
        on_progress: Callable[[ProgressState], None] | None = None,
        cancel_event: threading.Event | None = None,
        total: TreeStats | None = None,
    ) -> CopyResult:
        src_path, dst_path = Path(src), Path(dst)
        if not src_path.is_dir():
            raise CopyFailed(f"源目录不存在：{src_path}")
        assert_disjoint(src_path, dst_path)
        # 取消必须在动任何文件之前生效：否则一个很快的复制会让"取消"变成空操作
        if cancel_event is not None and cancel_event.is_set():
            raise CopyCancelled("复制在开始前已被取消")
        dst_path.mkdir(parents=True, exist_ok=True)

        log_path = dst_path.parent / f".steamboot_fastcopy_{int(time.time() * 1000)}.log"
        args = self.build_args(src_path, dst_path, mode=mode, verify=verify, log_path=log_path)
        log.info("FastCopy 启动：%s", " ".join(args))

        started = time.time()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creationflags)

        monitor_stop = threading.Event()
        monitor = None
        if on_progress is not None:
            monitor = threading.Thread(
                target=self._monitor_progress,
                args=(dst_path, total, on_progress, monitor_stop, cancel_event, started),
                daemon=True,
            )
            monitor.start()

        cancelled = False
        try:
            while True:
                try:
                    proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        self._terminate(proc)
                        break
        finally:
            monitor_stop.set()
            if monitor is not None:
                monitor.join(timeout=3)

        elapsed = time.time() - started
        out = proc.stdout.read() if proc.stdout else b""
        err = proc.stderr.read() if proc.stderr else b""
        exit_code = proc.returncode

        if cancelled:
            self._cleanup_log(log_path)
            raise CopyCancelled(f"FastCopy 已取消（已运行 {elapsed:.1f} 秒）")

        result = self._read_result(log_path, exit_code, elapsed)
        if on_progress is not None:
            on_progress(
                ProgressState(
                    percent=100.0 if result.ok else 0.0,
                    bytes_done=result.bytes_copied or (total.bytes if total else 0),
                    bytes_total=total.bytes if total else 0,
                    speed_bps=(result.bytes_copied / elapsed) if elapsed > 0 else 0.0,
                    elapsed=elapsed,
                )
            )
        self._cleanup_log(log_path)

        if not result.ok:
            detail = result.detail or err.decode("mbcs", errors="replace").strip()
            raise CopyFailed(
                f"FastCopy 复制失败（退出码 {exit_code}）：{detail}",
                detail=detail,
                exit_code=exit_code,
            )
        if out or err:  # FastCopy 正常时两者都为空，有输出说明有异常信息，记下来
            log.debug("FastCopy 输出：%r %r", out[:500], err[:500])
        return result

    def _read_result(self, log_path: Path, exit_code: int | None, elapsed: float) -> CopyResult:
        """读取并解析 FastCopy 日志，判定成功与否。"""
        text = ""
        if log_path.is_file():
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        info = parse_fastcopy_log(text)

        result = CopyResult(
            engine=self.name,
            exit_code=exit_code,
            elapsed=elapsed,
            log_path=str(log_path),
        )
        err_files = int(info.get("err_files", 0) or 0)
        err_dirs = int(info.get("err_dirs", 0) or 0)
        result.error_files = err_files

        if "result_line" not in info:
            result.ok = False
            result.detail = "未能从 FastCopy 日志读取结果行（可能未正常启动）"
            return result
        if exit_code not in (0, None):
            result.ok = False
            result.detail = f"退出码 {exit_code}；{info.get('result_line')}"
            return result
        if err_files or err_dirs:
            result.ok = False
            result.detail = f"{info.get('result_line')}"
            return result

        result.ok = True
        result.files_copied = int(info.get("TotalFiles", 0) or 0)
        result.files_skipped = int(info.get("SkipFiles", 0) or 0)
        result.files_deleted = int(info.get("DelFiles", 0) or 0)
        result.bytes_copied = _fc_size_to_bytes(str(info.get("TotalWrite", "0")))
        result.detail = (
            f"复制 {result.files_copied} 个文件 / {result.bytes_copied / 1048576:.1f} MB，"
            f"跳过 {result.files_skipped}，删除 {result.files_deleted}，"
            f"用时 {info.get('TotalTime', f'{elapsed:.1f} sec')}"
        )
        return result

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        """取消：先礼貌终止，1.5 秒内不退再强杀（FastCopy 可能正在写文件）。"""
        try:
            proc.terminate()
            proc.wait(timeout=1.5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
                proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, OSError):
                log.warning("FastCopy 进程未能终止，进程号 %s 需要人工检查", proc.pid)

    @staticmethod
    def _cleanup_log(log_path: Path) -> None:
        try:
            if log_path.is_file():
                log_path.unlink()
        except OSError:
            pass

    def _monitor_progress(
        self,
        dst: Path,
        total: TreeStats | None,
        on_progress: Callable[[ProgressState], None],
        stop_event: threading.Event,
        cancel_event: threading.Event | None,
        started: float,
    ) -> None:
        """轮询目标目录已落盘字节数，换算进度/速度/ETA。"""
        total_bytes = total.bytes if total else 0
        total_files = total.files if total else 0
        if total and total.total > POLL_ENTRY_LIMIT:
            log.info("目录条目过多（%d），本轮不轮询进度以免拖慢复制", total.total)
            return
        last_bytes, last_time = 0, started
        while not stop_event.wait(POLL_INTERVAL):
            if cancel_event is not None and cancel_event.is_set():
                return
            try:
                stats = scan_tree(dst)
            except OSError:
                continue
            now = time.time()
            delta_bytes, delta_time = stats.bytes - last_bytes, now - last_time
            speed = (delta_bytes / delta_time) if delta_time > 0.5 and delta_bytes > 0 else 0.0
            last_bytes, last_time = stats.bytes, now
            percent = (stats.bytes / total_bytes * 100.0) if total_bytes else 0.0
            eta = None
            if speed > 0 and total_bytes > stats.bytes:
                eta = (total_bytes - stats.bytes) / speed
            try:
                on_progress(
                    ProgressState(
                        percent=min(percent, 99.9),
                        bytes_done=stats.bytes,
                        bytes_total=total_bytes,
                        files_done=stats.files,
                        files_total=total_files,
                        speed_bps=speed,
                        eta_seconds=eta,
                        elapsed=now - started,
                    )
                )
            except Exception:  # noqa: BLE001 - 回调出错不能影响复制
                log.exception("进度回调异常")


def _fc_size_to_bytes(text: str) -> int:
    """把 FastCopy 日志里的 ``12.3 MiB`` / ``1,234`` 换算成字节。"""
    raw = (text or "").strip()
    if not raw:
        return 0
    match = re.match(r"([\d,\.]+)\s*([KMGT]?i?B)?", raw, re.IGNORECASE)
    if not match:
        return 0
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return 0
    unit = (match.group(2) or "").upper().replace("I", "")
    factor = {"": 1, "B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}.get(unit, 1)
    return int(value * factor)


# ------------------------------------------------------------------ robocopy
_ROBOCOPY_SUMMARY_KEYS = ("Dirs :", "Files :", "Bytes :", "Times :")


def _default_robocopy_path() -> str:
    """robocopy 的默认位置：PATH 优先，其次 ``%SystemRoot%\\System32``（不写死盘符）。"""
    found = shutil.which("robocopy")
    if found:
        return found
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or ""
    return str(Path(system_root) / "System32" / "Robocopy.exe") if system_root else "robocopy.exe"


class RobocopyEngine(BaseEngine):
    """robocopy 引擎（回退方案）。

    退出码是位标志：0-7 都算成功，>= 8 才是失败。
    实时进度来自 stdout 里每个文件的处理行。
    """

    name = "robocopy"
    label = "robocopy"

    def __init__(self, cfg: Config | None = None, executable: str | Path | None = None) -> None:
        super().__init__(cfg)
        found = str(executable) if executable else _default_robocopy_path()
        if not Path(found).is_file():
            raise CopyFailed("未找到 robocopy")
        self.executable = found

    def describe(self) -> str:
        return f"robocopy（{self.executable}）"

    def build_args(self, src: str | os.PathLike[str], dst: str | os.PathLike[str], *, mode: str, threads: int) -> list[str]:
        args = [
            str(self.executable),
            str(src),
            str(dst),
            "/BYTES",           # 字节为单位，便于解析
            "/COPY:DAT",        # 数据 + 属性 + 时间戳
            "/DCOPY:DAT",
            "/R:2",             # 失败重试 2 次
            "/W:2",             # 每次等待 2 秒
            "/NP",              # 不显示百分比（我们自己算进度）
            "/NDL",             # 不列目录
            "/TEE",             # 输出同时写控制台，便于我们读取
        ]
        args.append("/MIR" if mode == MODE_MIRROR else "/E")
        if threads and threads > 1:
            args.append(f"/MT:{max(1, min(int(threads), 128))}")
        return args

    def copy_tree(
        self,
        src: str | os.PathLike[str],
        dst: str | os.PathLike[str],
        *,
        mode: str = MODE_DIFF,
        verify: bool = False,
        on_progress: Callable[[ProgressState], None] | None = None,
        cancel_event: threading.Event | None = None,
        total: TreeStats | None = None,
    ) -> CopyResult:
        src_path, dst_path = Path(src), Path(dst)
        if not src_path.is_dir():
            raise CopyFailed(f"源目录不存在：{src_path}")
        assert_disjoint(src_path, dst_path)
        if cancel_event is not None and cancel_event.is_set():
            raise CopyCancelled("复制在开始前已被取消")
        dst_path.mkdir(parents=True, exist_ok=True)

        args = self.build_args(src_path, dst_path, mode=mode, threads=self.cfg.robocopy_threads)
        log.info("robocopy 启动：%s", " ".join(args))
        started = time.time()

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )

        total_bytes = total.bytes if total else 0
        total_files = total.files if total else 0
        copied_bytes = 0
        copied_files = 0
        last_report = started
        window_bytes = 0
        window_start = started
        lines: list[str] = []
        cancelled = False

        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("mbcs", errors="replace").rstrip("\r\n")
            if line:
                lines.append(line)
                if len(lines) > 4000:      # 只保留尾部，防止超大输出吃内存
                    del lines[:2000]
            # 形如:  "100%  New File  \t  12345678 \t path"
            if "New File" in line or "Newer" in line or "100%" in line:
                match = re.search(r"(\d{4,})\s+([A-Za-z]:\\|\\\\)", line)
                if match:
                    size = int(match.group(1))
                    copied_bytes += size
                    copied_files += 1
                    window_bytes += size
                    now = time.time()
                    if on_progress is not None and now - last_report >= 0.5:
                        span = max(now - window_start, 0.001)
                        speed = window_bytes / span
                        percent = (copied_bytes / total_bytes * 100.0) if total_bytes else 0.0
                        eta = ((total_bytes - copied_bytes) / speed) if speed > 0 and total_bytes > copied_bytes else None
                        try:
                            on_progress(
                                ProgressState(
                                    percent=min(percent, 99.9),
                                    bytes_done=copied_bytes,
                                    bytes_total=total_bytes,
                                    files_done=copied_files,
                                    files_total=total_files,
                                    speed_bps=speed,
                                    eta_seconds=eta,
                                    elapsed=now - started,
                                )
                            )
                        except Exception:  # noqa: BLE001
                            log.exception("进度回调异常")
                        last_report = now
                        window_bytes = 0
                        window_start = now
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                self._terminate(proc)
                break

        proc.wait()
        elapsed = time.time() - started
        exit_code = proc.returncode

        if cancelled:
            raise CopyCancelled(f"robocopy 已取消（已运行 {elapsed:.1f} 秒）")

        if exit_code is None or exit_code >= 8:
            detail = "\n".join(lines[-20:])
            raise CopyFailed(f"robocopy 失败（退出码 {exit_code}）", detail=detail, exit_code=exit_code)

        result = CopyResult(
            engine=self.name,
            ok=True,
            exit_code=exit_code,
            bytes_copied=copied_bytes or (total.bytes if total else 0),
            files_copied=copied_files,
            elapsed=elapsed,
            detail=f"robocopy 退出码 {exit_code}（0-7 均为成功）",
        )
        if on_progress is not None:
            try:
                on_progress(
                    ProgressState(
                        percent=100.0,
                        bytes_done=result.bytes_copied,
                        bytes_total=total_bytes,
                        files_done=copied_files,
                        files_total=total_files,
                        speed_bps=(result.bytes_copied / elapsed) if elapsed > 0 else 0.0,
                        elapsed=elapsed,
                    )
                )
            except Exception:  # noqa: BLE001
                log.exception("进度回调异常")
        return result

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
            except OSError:
                pass


# ------------------------------------------------------------------ 工厂
def detect_engine(cfg: Config | None = None, prefer: str = "") -> BaseEngine:
    """按配置/可用性选择引擎：FastCopy 优先，找不到自动回退 robocopy。"""
    config = cfg or Config.load()
    wanted = (prefer or config.copy_engine or "").strip().lower()
    if wanted in ("", "fastcopy", "auto"):
        try:
            return FastCopyEngine(config, config.fastcopy_path or None)
        except CopyFailed as exc:
            log.warning("FastCopy 不可用（%s），回退 robocopy", exc)
            return RobocopyEngine(config)
    if wanted == "robocopy":
        return RobocopyEngine(config)
    raise CopyFailed(f"未知的复制引擎：{wanted}")


def engine_summary(cfg: Config | None = None) -> str:
    """给人看的一句话：**实际**会用哪个引擎（含"已回退"的明确说明）。

    界面状态栏与 ``--selftest`` 都用它，避免"选了 FastCopy 却在跑 robocopy"
    这种只有看日志才知道的静默降级。
    """
    config = cfg or Config.load()
    wanted = (config.copy_engine or "").strip().lower()
    try:
        engine = detect_engine(config)
    except CopyFailed as exc:  # 未知引擎名等配置错误
        return f"不可用（{exc}）"
    if engine.name == "robocopy" and wanted not in ("robocopy",):
        return "robocopy（未找到 FastCopy，已自动回退）"
    return engine.describe()


__all__ = [
    "BaseEngine",
    "CopyCancelled",
    "CopyFailed",
    "CopyResult",
    "FastCopyEngine",
    "MODE_DIFF",
    "MODE_MIRROR",
    "ProgressState",
    "RobocopyEngine",
    "TreeStats",
    "VerifyResult",
    "assert_disjoint",
    "detect_engine",
    "engine_summary",
    "fastcopy_available",
    "fastcopy_candidate_dirs",
    "find_fastcopy",
    "normalize_fastcopy_dest",
    "parse_fastcopy_log",
    "path_from_registry_value",
    "plan_extras",
    "registry_fastcopy_dirs",
    "scan_tree",
    "verify_trees",
]
