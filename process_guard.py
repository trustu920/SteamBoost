"""进程安全检查 —— 任何会改动 Steam 文件结构的操作前都必须通过。

规则（需求 3.1 / 3.2 与"全局安全检查"）：
- 加速与释放前，Steam 客户端必须未运行；
- 目标游戏目录下的任何可执行文件不能正在运行（游戏进程不一定叫游戏名，
  所以按"进程映像路径是否位于游戏目录之内"来判断，而不是按进程名）；
- 检查失败时**抛异常中止**，绝不"提示后继续"。

``steamservice.exe`` 是 Windows 服务，常年常驻，刻意不列为阻塞项。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from junction_utils import is_within, normalize_for_compare

__all__ = [
    "PreflightError",
    "PreflightResult",
    "STEAM_CLIENT_NAMES",
    "game_processes",
    "is_steam_running",
    "preflight",
    "running_processes_under",
]

#: 视为"Steam 客户端在运行"的进程名（小写）
STEAM_CLIENT_NAMES = {"steam.exe", "steamwebhelper.exe"}


class PreflightError(RuntimeError):
    """前置检查未通过。``blockers`` 里是可以直接展示给用户的中文原因。"""

    def __init__(self, message: str, blockers: list[str] | None = None) -> None:
        super().__init__(message)
        self.blockers = blockers or []


@dataclass
class PreflightResult:
    """前置检查结果，供 GUI 展示与日志记录。"""

    steam_running: bool = False
    steam_pids: list[int] = field(default_factory=list)
    game_processes: list[tuple[int, str, str]] = field(default_factory=list)  # (pid, 名称, 路径)
    blockers: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers


def _psutil():
    """延迟导入 psutil，缺失时给出明确的中文错误。"""
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise PreflightError(
            "缺少 psutil 依赖，无法进行进程安全检查。请执行：pip install psutil"
        ) from exc
    return psutil


def _iter_processes():
    """遍历进程；权限不足的条目直接跳过，不让检查本身抛错。"""
    psutil = _psutil()
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            yield proc
        except Exception:  # noqa: BLE001 - 单个进程异常不应影响整体扫描
            continue


def is_steam_running() -> tuple[bool, list[int]]:
    """返回 ``(Steam 客户端是否在运行, 相关进程号)``。"""
    pids: list[int] = []
    for proc in _iter_processes():
        try:
            name = (proc.info.get("name") or "").lower()
        except Exception:  # noqa: BLE001
            continue
        if name in STEAM_CLIENT_NAMES:
            pids.append(int(proc.info.get("pid") or 0))
    return bool(pids), pids


def running_processes_under(root: str | os.PathLike[str]) -> list[tuple[int, str, str]]:
    """列出映像路径位于 root 之内的进程 ``(pid, 名称, 路径)``。

    这是判定"游戏正在运行"的正确方式：进程名与游戏名/目录名往往不一致，
    但它的可执行文件一定在游戏目录里。
    """
    root_path = Path(os.path.abspath(str(root)))
    if not root_path.is_dir():
        return []
    found: list[tuple[int, str, str]] = []
    for proc in _iter_processes():
        try:
            info = proc.info
            exe = info.get("exe") or ""
            if not exe:
                continue
            if is_within(exe, root_path):
                found.append((int(info.get("pid") or 0), str(info.get("name") or ""), str(exe)))
        except Exception:  # noqa: BLE001
            continue
    return found


def game_processes(game_path: str | os.PathLike[str]) -> list[tuple[int, str, str]]:
    """列出正在运行的该游戏进程（别名，语义更清晰）。"""
    return running_processes_under(game_path)


def preflight(
    game_path: str | os.PathLike[str] | None = None,
    *,
    require_steam_closed: bool = True,
    extra_paths: list[str] | None = None,
) -> PreflightResult:
    """执行一次完整的前置检查，返回结果（不抛异常，便于 GUI 展示）。

    调用方在真正动手前应使用 :func:`ensure_safe` 或自行检查 ``result.ok``。
    """
    result = PreflightResult()

    if require_steam_closed:
        running, pids = is_steam_running()
        result.steam_running, result.steam_pids = running, pids
        if running:
            result.blockers.append(
                f"Steam 客户端正在运行（进程号 {', '.join(str(p) for p in pids)}），请先完全退出 Steam"
            )

    targets: list[str] = []
    if game_path:
        targets.append(str(game_path))
    targets.extend(extra_paths or [])

    for target in targets:
        for pid, name, exe in running_processes_under(target):
            result.game_processes.append((pid, name, exe))
    if result.game_processes:
        detail = "；".join(f"{name}(PID {pid})" for pid, name, _ in result.game_processes[:5])
        result.blockers.append(f"游戏进程正在运行：{detail}，请先退出游戏")

    return result


def ensure_safe(
    game_path: str | os.PathLike[str] | None = None,
    *,
    require_steam_closed: bool = True,
    extra_paths: list[str] | None = None,
) -> PreflightResult:
    """前置检查，未通过时抛 :class:`PreflightError`（需求要求"中止并说明原因"）。"""
    result = preflight(game_path, require_steam_closed=require_steam_closed, extra_paths=extra_paths)
    if not result.ok:
        raise PreflightError("前置检查未通过：" + "；".join(result.blockers), result.blockers)
    return result


def same_process_alive(pid: int) -> bool:
    """判断某个进程号是否仍在运行（取消/回滚时用来确认已退出）。"""
    psutil = _psutil()
    return psutil.pid_exists(pid)


def normalize_path(path: str | os.PathLike[str]) -> str:
    """供日志/比较使用的规范路径。"""
    return normalize_for_compare(path)
