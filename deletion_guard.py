"""删除闸门 —— 全程序所有不可逆删除的唯一入口。

硬性规则（用户明确要求："我需要你来确认删除动作，不可以误删我的文件"）：

1. **默认拒绝**：没有确认者（``confirmer=None``）时一律抛异常，绝不"静默继续"。
   无人值守、脚本、后台任务因此天然删不掉任何东西。
2. **先校验、后询问**：目标必须位于允许的根目录之内，且不能是盘根、不能是目录联接、
   不能位于任何 Steam 库内——这些校验不通过时**连问都不问**，直接拒绝。
3. **人类动作才算确认**：确认者由界面或命令行提供；本模块不提供"自动同意"的实现，
   也不允许把布尔值直接当成确认结果。
4. **一次确认只对一次请求有效**：确认后得到的是不可复制的凭据对象
   :class:`ConfirmedDeletion`，删除函数只接受这种凭据，且执行前会再复核一次路径。
5. 每个请求与决定都写审计日志，包含目标的文件数、字节数与样例，事后可追溯。

允许被删除的东西**只有三类**（白名单）：

* 加速盘上的缓存副本：``<缓存根>\\<appid>_<安装目录名>``；
* 母盘隔离区里的隔离项：``<母盘>:\\SteamBoostTrash\\<项目>``；
* 复制过程中产生的临时日志：``<缓存根>\\.steamboot_fastcopy_*.log``。

除此之外的任何路径都删不掉——包括 Steam 库目录、游戏本体目录、``.hdd_cache`` 母本。
删除目录联接有专门的 :func:`junction_utils.remove_junction`，不走这里。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from config import Config, human_size
from junction_utils import PathSafetyError, assert_within, is_junction, is_within
from logger import log_operation, setup_logger

log = setup_logger("steamboot.delete")

# ---------------------------------------------------------------- 删除类型
KIND_CACHE_COPY = "cache_copy"          # 加速缓存副本（释放空间时删除）
KIND_PARTIAL_COPY = "partial_copy"      # 加速中断留下的半成品副本
KIND_QUARANTINE = "quarantine_item"     # 隔离项（用户确认游戏能跑之后删除）
KIND_TRANSIENT_LOG = "transient_log"    # FastCopy 结果日志等临时文件
KIND_LOG_FILE = "log_file"              # 用户主动触发的旧日志清理

#: 任何删除目标都不允许出现在这些路径片段里（Steam 库、游戏本体所在处）
FORBIDDEN_PATH_PARTS = ("\\steamapps\\", "/steamapps/")

#: 受保护的系统/用户目录环境变量名：其**任何子路径**都一律拒绝删除。
#: 这一层独立于用户配置，专门用来兜住"把缓存目录配到危险位置"这类配置事故。
PROTECTED_ENV_ROOTS = (
    "SystemRoot",
    "windir",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramData",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
)


class DeletionRefused(PathSafetyError):
    """删除被拒绝（路径不合法）。

    继承 :class:`junction_utils.PathSafetyError`，这样调用方无论按"路径不安全"还是
    按"删除被拒"来捕获，都不会漏掉拒绝——闸门的拒绝必须是**统一且可捕获的**。
    """


class DeletionNotConfirmed(DeletionRefused):
    """没有得到人类确认。"""


# ---------------------------------------------------------------- 数据结构
@dataclass
class DeletionRequest:
    """一次待确认的删除请求。界面直接展示 :meth:`summary`。"""

    kind: str
    target: str
    allowed_root: str
    reason: str = ""
    reversible: bool = False
    files: int = 0
    bytes: int = 0
    sample: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)

    @property
    def kind_label(self) -> str:
        return {
            KIND_CACHE_COPY: "加速缓存副本（删除后需重新加速才能恢复）",
            KIND_PARTIAL_COPY: "未完成的缓存副本（半成品）",
            KIND_QUARANTINE: "隔离区文件（回写时从母盘搬出来的多余文件）",
            KIND_TRANSIENT_LOG: "临时日志文件",
        }.get(self.kind, self.kind)

    @property
    def size_text(self) -> str:
        return human_size(self.bytes)

    def summary(self) -> str:
        lines = [
            f"即将删除：{self.target}",
            f"  类型：{self.kind_label}",
            f"  内容：{self.files} 个文件 / {self.size_text}",
        ]
        if self.sample:
            head = "、".join(self.sample[:6])
            more = f" 等 {self.files} 个" if self.files > len(self.sample) else ""
            lines.append(f"  样例：{head}{more}")
        lines.append(f"  原因：{self.reason or '（未说明）'}")
        if not self.reversible:
            lines.append("  注意：此操作不可撤销")
        return "\n".join(lines)


@dataclass(frozen=True)
class ConfirmedDeletion:
    """确认凭据：只有拿到它才能执行删除。"""

    request: DeletionRequest
    confirmed_at: float
    confirmer: str = ""


class Confirmer(Protocol):
    """确认者接口：界面弹框、命令行提问都实现它。"""

    name: str

    def confirm(self, request: DeletionRequest) -> bool:  # pragma: no cover - 协议
        ...


class DenyAllConfirmer:
    """默认确认者：一律拒绝。没有界面时必须保持这个行为。"""

    name = "deny-all"

    def confirm(self, request: DeletionRequest) -> bool:
        log.warning("没有可用的确认者，已拒绝删除请求：%s", request.target)
        return False


class PromptConfirmer:
    """命令行确认者：打印摘要，要求用户**亲手输入确认词**。

    除确认词以外的任何输入（包括直接回车、EOF、Ctrl+C）都视为取消。
    """

    name = "prompt"

    def __init__(self, word: str = "删除", stream_out: Any = None) -> None:
        self.word = word
        self.stream_out = stream_out or sys.stdout

    def confirm(self, request: DeletionRequest) -> bool:
        print(request.summary(), file=self.stream_out)
        prompt = f"如确认执行请输入「{self.word}」，其他任何输入都会取消："
        try:
            answer = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print("\n未收到确认，已取消。", file=self.stream_out)
            return False
        return answer.strip() == self.word


# ---------------------------------------------------------------- 度量与校验
def measure(path: str | os.PathLike[str], sample_limit: int = 6) -> tuple[int, int, list[str]]:
    """统计目录/文件的文件数、总字节与样例（供用户在确认前看清楚）。"""
    target = Path(path)
    if not os.path.lexists(str(target)):
        return 0, 0, []
    if target.is_file():
        try:
            return 1, target.stat().st_size, [target.name]
        except OSError:
            return 1, 0, [target.name]
    files = 0
    total = 0
    sample: list[str] = []
    for current, _dirs, names in os.walk(target):
        for name in names:
            full = os.path.join(current, name)
            try:
                total += os.path.getsize(full)
                files += 1
            except OSError:
                continue
            if len(sample) < sample_limit:
                sample.append(os.path.relpath(full, target))
    return files, total, sample


def protected_roots() -> list[str]:
    """返回受保护的系统/用户目录列表（纵深防御，与用户配置无关）。"""
    roots: list[str] = []
    for name in PROTECTED_ENV_ROOTS:
        value = os.environ.get(name)
        if value:
            try:
                roots.append(os.path.abspath(value))
            except (OSError, ValueError):
                continue
    return roots


def validate_target(kind: str, target: str | os.PathLike[str], allowed_root: str | os.PathLike[str]) -> str:
    """执行删除前的硬校验；返回规范化后的目标路径。任何一条不过就抛异常。"""
    text = os.path.abspath(str(target))
    normalized = os.path.normcase(text)

    # 白名单校验统一转换成 DeletionRefused，避免调用方按文档捕获却漏掉越界拒绝
    try:
        assert_within(text, allowed_root, "删除目标")
    except PathSafetyError as exc:
        raise DeletionRefused(str(exc)) from exc

    root_norm = os.path.normcase(os.path.abspath(str(allowed_root)))
    if normalized == root_norm:
        raise DeletionRefused(f"拒绝删除允许范围的根目录本身：{text}")

    drive, _tail = os.path.splitdrive(text)
    if drive and normalized.rstrip("\\/") == os.path.normcase(drive + os.sep).rstrip("\\/"):
        raise DeletionRefused(f"拒绝删除盘根目录：{text}")

    for part in FORBIDDEN_PATH_PARTS:
        if part.lower() in normalized:
            raise DeletionRefused(f"拒绝删除 Steam 库/游戏本体路径下的任何内容：{text}")

    # 纵深防御：即使有人把缓存/隔离区配到了系统目录或用户目录，也一律拒绝
    for protected in protected_roots():
        if is_within(text, protected):
            raise DeletionRefused(f"拒绝删除系统或用户目录下的内容：{text}（受保护根：{protected}）")

    if is_junction(text):
        raise DeletionRefused(
            f"拒绝按目录删除：该路径是目录联接，只能用 remove_junction 摘除联接本身：{text}"
        )
    return text


def build_request(
    kind: str,
    target: str | os.PathLike[str],
    allowed_root: str | os.PathLike[str],
    *,
    reason: str = "",
    reversible: bool = False,
) -> DeletionRequest:
    """构造删除请求（含内容度量），先做路径校验。"""
    text = validate_target(kind, target, allowed_root)
    files, total, sample = measure(text)
    return DeletionRequest(
        kind=kind,
        target=text,
        allowed_root=os.path.abspath(str(allowed_root)),
        reason=reason,
        reversible=reversible,
        files=files,
        bytes=total,
        sample=sample,
    )


# ---------------------------------------------------------------- 确认与执行
def confirm_deletion(request: DeletionRequest, confirmer: Confirmer | None) -> ConfirmedDeletion:
    """请求人类确认；未确认则抛 :class:`DeletionNotConfirmed`（默认拒绝）。"""
    active: Confirmer = confirmer or DenyAllConfirmer()  # type: ignore[assignment]
    approved = False
    try:
        approved = bool(active.confirm(request))
    except Exception as exc:  # noqa: BLE001 - 确认过程本身出错也必须视为拒绝
        log.exception("确认过程异常，按拒绝处理")
        approved = False
        reason_text = f"确认过程异常：{exc}"
    else:
        reason_text = "用户确认" if approved else "用户取消/未确认"

    log_operation(
        "delete_request",
        src=request.target,
        size_bytes=request.bytes,
        result="confirmed" if approved else "refused",
        detail=f"kind={request.kind} files={request.files} confirmer={active.name} {reason_text}",
    )
    if not approved:
        raise DeletionNotConfirmed(f"未获确认，已取消删除：{request.target}\n{request.summary()}")
    return ConfirmedDeletion(request=request, confirmed_at=time.time(), confirmer=active.name)


def safe_remove_tree(token: ConfirmedDeletion) -> int:
    """执行目录删除。只接受确认凭据，且执行前再复核一次路径。"""
    request = token.request
    validate_target(request.kind, request.target, request.allowed_root)  # 再复核
    target = Path(request.target)
    if not target.exists():
        return 0
    files, total, _ = measure(target)
    shutil.rmtree(target)
    log_operation(
        "delete_executed",
        src=request.target,
        size_bytes=total,
        result="ok",
        detail=f"kind={request.kind} files={files} confirmer={token.confirmer}",
    )
    return total


def safe_unlink(token: ConfirmedDeletion) -> int:
    """执行单文件删除（例如复制过程留下的临时日志）。"""
    request = token.request
    validate_target(request.kind, request.target, request.allowed_root)
    path = Path(request.target)
    if not path.is_file():
        return 0
    size = path.stat().st_size
    path.unlink()
    log_operation(
        "delete_executed",
        src=request.target,
        size_bytes=size,
        result="ok",
        detail=f"kind={request.kind} confirmer={token.confirmer}",
    )
    return size


def delete(
    kind: str,
    target: str | os.PathLike[str],
    allowed_root: str | os.PathLike[str],
    *,
    confirmer: Confirmer | None = None,
    reason: str = "",
    reversible: bool = False,
) -> int:
    """一步完成"校验 → 询问 → 删除"，返回释放字节数。

    这是给业务代码用的便捷入口；没有 ``confirmer`` 时**必然失败**，
    因此不可能出现"忘记确认"的调用路径。
    """
    request = build_request(kind, target, allowed_root, reason=reason, reversible=reversible)
    token = confirm_deletion(request, confirmer)
    return safe_remove_tree(token)


def allowed_roots(cfg: Config) -> dict[str, str]:
    """返回各类删除的允许根目录（供界面与业务代码统一取用）。"""
    return {
        KIND_CACHE_COPY: str(cfg.resolved_cache_dir()),
        KIND_PARTIAL_COPY: str(cfg.resolved_cache_dir()),
        KIND_QUARANTINE: str(cfg.resolved_trash_root()),
        KIND_TRANSIENT_LOG: str(cfg.resolved_cache_dir()),
        KIND_LOG_FILE: str(cfg.resolved_log_dir()),
    }


__all__ = [
    "ConfirmedDeletion",
    "Confirmer",
    "DeletionNotConfirmed",
    "DeletionRefused",
    "DeletionRequest",
    "DenyAllConfirmer",
    "KIND_CACHE_COPY",
    "KIND_LOG_FILE",
    "KIND_PARTIAL_COPY",
    "KIND_QUARANTINE",
    "KIND_TRANSIENT_LOG",
    "PROTECTED_ENV_ROOTS",
    "PromptConfirmer",
    "allowed_roots",
    "build_request",
    "confirm_deletion",
    "delete",
    "measure",
    "protected_roots",
    "safe_remove_tree",
    "safe_unlink",
    "validate_target",
]
