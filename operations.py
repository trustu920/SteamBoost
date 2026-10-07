"""三个核心操作：加速 / 回写 / 释放。

流程与安全约束（对应需求 3.1 / 3.2，以及用户后续确认的改法）
-----------------------------------------------------------

**加速（母盘 → 加速盘）**

1. 前置检查：Steam 与游戏进程未运行、游戏在母盘上且是真实目录、备份路径未被占用、
   缓存空间 > 游戏大小 × 1.05、缓存目录合法；
2. 原目录改名为 ``<库>\\steamapps\\common\\.hdd_cache\\<installdir>``（母本先落袋为安）；
3. 复制母本 → 缓存副本（差异模式，可断点续拷；进度/速度/ETA 实时回报，可取消）；
4. 校验（文件数 + 总字节 + 逐文件大小/时间戳，严格模式）；
5. 原位置创建目录联接（Junction）指向缓存副本，创建后回读验证；
6. 状态写 ``state.json``，全程写审计日志。

任一步失败或用户取消 → **完整回滚**：删掉半成品副本（需确认）、把母本改回原名。
若用户拒绝删除半成品，则保留副本并标记为失败待修复，交给修复向导。

**回写（加速盘 → 母盘，随时可点，保持加速状态）**

1. 前置检查：进程未运行、联接有效且指向记录的缓存副本、母本备份存在；
2. 算出"母盘多出来、需要搬走"的文件清单 → **先展示给你并等你确认**（可取消）；
3. 把这些文件**移动**到隔离区（``<母盘>:\\SteamBoostTrash\\…``，同卷改名，不复制数据）；
4. 镜像复制：缓存副本 → 母本（``/cmd=sync``），此时母盘已不多任何文件，**全程无删除**；
5. 严格校验两侧完全一致；
6. 状态记录最近回写时间与隔离项，**保持加速状态不变**。

**释放（回写 + 撤掉加速）**

1. 前置检查同上；
2. **先取删除凭据**：把"要删除缓存副本 X（N 个文件 / Y）"交给你确认，
   若你拒绝则立即中止，此时**什么都还没动**；
3. 执行一次回写（含上面的多余文件确认）；
4. 删除目录联接（只摘联接本身）；
5. 母本从 ``.hdd_cache`` 改名回原位置 ``common\\<installdir>``；
6. 用第 2 步拿到的凭据删除缓存副本，状态回到"母盘中"。
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from config import HDD_CACHE_DIR_NAME, Config, human_size
from copy_engine import (
    MODE_DIFF,
    MODE_MIRROR,
    CopyCancelled,
    CopyFailed,
    ProgressState,
    assert_disjoint,
    detect_engine,
    plan_extras,
    scan_tree,
    verify_trees,
)
from deletion_guard import (
    KIND_CACHE_COPY,
    KIND_PARTIAL_COPY,
    DeletionNotConfirmed,
    build_request,
    confirm_deletion,
    safe_remove_tree,
)
from junction_utils import (
    JunctionError,
    assert_junction_points_to,
    create_junction,
    is_junction,
    junction_target,
    remove_junction,
)
from logger import log_operation, setup_logger
from process_guard import PreflightError, ensure_safe, preflight
from quarantine import quarantine_extras
from state import (
    PHASE_ACCELERATED,
    PHASE_ACCELERATING,
    PHASE_FAILED,
    PHASE_RELEASING,
    PHASE_WRITING_BACK,
    StateStore,
    build_task,
)

log = setup_logger("steamboot.ops")


class OperationBlocked(RuntimeError):
    """前置检查未通过，操作未开始。``blockers`` 可直接展示给用户。"""

    def __init__(self, message: str, blockers: list[str] | None = None) -> None:
        super().__init__(message)
        self.blockers = blockers or []


class OperationFailed(RuntimeError):
    """操作中途失败；``detail`` 说明现场状态与是否已回滚。"""

    def __init__(self, message: str, detail: str = "", rolled_back: bool = False) -> None:
        super().__init__(message)
        self.detail = detail
        self.rolled_back = rolled_back


class ExtrasConfirmer(Protocol):
    """确认"把母盘多余文件搬进隔离区"的接口（界面弹框 / 命令行提问实现它）。"""

    name: str

    def confirm_extras(self, game_name: str, extras: list[str], total_bytes: int) -> bool:  # pragma: no cover
        ...


class DenyAllExtrasConfirmer:
    """默认：一律拒绝搬运。没有界面时保持这个行为（不动物任何文件）。"""

    name = "deny-all"

    def confirm_extras(self, game_name: str, extras: list[str], total_bytes: int) -> bool:
        log.warning("没有可用的确认者，已拒绝搬移母盘多余文件（%d 个）", len(extras))
        return False


class AutoExtrasConfirmer:
    """供命令行交互使用：把清单打印出来，等用户回答。"""

    name = "prompt"

    def confirm_extras(self, game_name: str, extras: list[str], total_bytes: int) -> bool:
        print(f"\n【{game_name}】回写前需要先把母盘上多出来的 {len(extras)} 个条目搬进隔离区")
        print(f"  合计：{human_size(total_bytes)}")
        for name in extras[:20]:
            print(f"    - {name}")
        if len(extras) > 20:
            print(f"    … 其余 {len(extras) - 20} 个")
        print("  说明：这些文件不会被删除，只是搬到隔离区，你可以随时还原。")
        try:
            answer = input("  如同意搬移请输入「继续」：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  未收到确认，已取消。")
            return False
        return answer == "继续"


@dataclass
class OperationResult:
    """一次操作的结果，界面直接展示。"""

    ok: bool
    action: str
    appid: str
    name: str = ""
    message: str = ""
    detail: str = ""
    backup_path: str = ""
    cache_copy: str = ""
    junction: str = ""
    quarantine_item: str = ""
    files: int = 0
    bytes_copied: int = 0
    rolled_back: bool = False
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ 前置检查
@dataclass
class CheckResult:
    ok: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def preflight_accelerate(game, cfg: Config) -> CheckResult:
    """加速前检查；任何一条不满足都不得开始。"""
    blockers: list[str] = []
    warnings: list[str] = []

    game_path = Path(game.game_path) if game.game_path else None
    backup = Path(game.hdd_backup_path) if game.hdd_backup_path else None
    cache_copy = Path(game.cache_copy_path) if game.cache_copy_path else None

    if not game.installdir:
        blockers.append("该游戏没有安装目录名（清单异常），无法加速")
    if game_path is None or not game_path.exists():
        blockers.append(f"游戏目录不存在：{game_path}")
    elif is_junction(game_path):
        blockers.append(f"该游戏已经处于加速状态：{game_path}")
    elif not game_path.is_dir():
        blockers.append(f"游戏路径不是目录：{game_path}")

    if backup is not None and backup.exists():
        blockers.append(f"母本备份目录已存在，请先用修复向导处理：{backup}")

    try:
        ensure_safe(game_path, require_steam_closed=True)
    except PreflightError as exc:
        blockers.extend(exc.blockers)

    problems, warn = cfg.validate(_libraries_of(game))
    blockers.extend(problems)
    warnings.extend(warn)

    cache_root = cfg.resolved_cache_dir()
    if not cache_root.exists():
        warnings.append(f"缓存目录尚不存在，将自动创建：{cache_root}")
    free = -1
    try:
        free = shutil.disk_usage(f"{os.path.splitdrive(str(cache_root))[0]}\\").free
    except OSError:
        blockers.append(f"无法读取加速盘剩余空间：{cache_root}")

    game_size = int(game.size_on_disk or 0)
    required = int(game_size * 1.05)
    if free >= 0 and game_size > 0 and free < required:
        blockers.append(
            f"加速盘剩余空间不足：需要 {human_size(required)}（游戏 {human_size(game_size)} × 1.05），"
            f"当前可用 {human_size(free)}，请先释放其他游戏"
        )

    if cache_copy is not None and cache_copy.exists():
        warnings.append(f"缓存副本目录已存在，将执行差异复制（自动跳过相同文件）：{cache_copy}")

    warnings.append("操作期间请不要启动 Steam，否则 Steam 会认为该游戏未安装")
    return CheckResult(ok=not blockers, blockers=blockers, warnings=warnings)


def preflight_accelerated(game, cfg: Config) -> CheckResult:
    """回写/释放前检查：联接必须有效、母本备份必须在。"""
    blockers: list[str] = []
    warnings: list[str] = []

    game_path = Path(game.game_path) if game.game_path else None
    backup = Path(game.hdd_backup_path) if game.hdd_backup_path else None
    cache_copy = Path(game.cache_copy_path) if game.cache_copy_path else None

    if game_path is None or not is_junction(game_path):
        blockers.append(f"原位置不是目录联接，无法回写或释放：{game_path}")
    if cache_copy is None or not cache_copy.is_dir():
        blockers.append(f"SSD 缓存副本不存在：{cache_copy}")
    if backup is None or not backup.is_dir():
        blockers.append(f"母本备份不存在（应先走修复向导）：{backup}")

    if game_path is not None and is_junction(game_path) and cache_copy is not None:
        actual = junction_target(game_path)
        if os.path.normcase(os.path.abspath(actual)) != os.path.normcase(os.path.abspath(str(cache_copy))):
            blockers.append(f"联接指向 {actual}，与记录的缓存副本 {cache_copy} 不一致，请先修复")

    try:
        ensure_safe(game_path, require_steam_closed=True)
    except PreflightError as exc:
        blockers.extend(exc.blockers)

    if cache_copy is not None and backup is not None and cache_copy.is_dir() and backup.is_dir():
        try:
            assert_disjoint(cache_copy, backup)
        except CopyFailed as exc:
            blockers.append(str(exc))

    return CheckResult(ok=not blockers, blockers=blockers, warnings=warnings)


def _libraries_of(game) -> list[tuple[str, str]]:
    """把游戏所在库换算成 ``(库路径, 盘符)``，供配置校验使用。"""
    library = str(game.library_path or "")
    drive = os.path.splitdrive(library)[0].rstrip(":\\").upper() if library else ""
    return [(library, drive)] if library else []


def _require(check: CheckResult, what: str) -> None:
    if not check.ok:
        raise OperationBlocked(f"{what}前置检查未通过：" + "；".join(check.blockers), check.blockers)


# ------------------------------------------------------------------ 加速
def accelerate(
    game,
    cfg: Config,
    *,
    store: StateStore | None = None,
    on_progress: Callable[[ProgressState], None] | None = None,
    cancel_event=None,
    deletion_confirmer=None,
    engine=None,
) -> OperationResult:
    """把母盘上的游戏加速到 SSD（母本改名 → 复制 → 校验 → 建联接）。"""
    state = store or StateStore(cfg=cfg)
    _require(preflight_accelerate(game, cfg), "加速")

    cache_root = cfg.resolved_cache_dir()
    cache_root.mkdir(parents=True, exist_ok=True)
    task = build_task(game, cfg)
    game_path = Path(task.junction)
    backup = Path(task.mother_path)
    cache_copy = Path(task.cache_copy)
    total = scan_tree(game_path)

    state.upsert(task)
    state.set_phase(task.appid, PHASE_ACCELERATING)
    log_operation(
        "accelerate_start",
        appid=task.appid,
        name=task.name,
        src=str(game_path),
        dst=str(cache_copy),
        size_bytes=total.bytes,
        result="running",
        detail=f"files={total.files}",
    )

    # 步骤 1：母本改名（同卷改名，瞬间完成）
    try:
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.rename(game_path, backup)
    except OSError as exc:
        state.set_phase(task.appid, PHASE_FAILED, notes=f"母本改名失败：{exc}")
        raise OperationFailed(f"母本改名失败：{exc}", rolled_back=False) from exc

    if not backup.is_dir() or game_path.exists():
        state.set_phase(task.appid, PHASE_FAILED, notes="母本改名后校验失败")
        raise OperationFailed("母本改名后校验失败，状态异常，请用修复向导处理")

    # 步骤 2~3：复制 + 校验；失败则回滚
    try:
        active_engine = engine or detect_engine(cfg)
        result = active_engine.copy_tree(
            backup,
            cache_copy,
            mode=MODE_DIFF,
            verify=cfg.fastcopy_verify,
            on_progress=on_progress,
            cancel_event=cancel_event,
            total=total,
        )
        verify = verify_trees(backup, cache_copy, allow_extra=False)
        if not verify.ok:
            raise OperationFailed(
                f"复制完成但校验未通过：{verify.detail}",
                detail="已回滚，游戏仍在母盘原状",
            )
    except (CopyCancelled, CopyFailed, OperationFailed) as exc:
        rolled = _rollback_accelerate(state, task, game_path, backup, cache_copy, deletion_confirmer)
        detail = "已回滚，游戏仍在母盘原状" if rolled else "回滚未完成：请用修复向导处理"
        log_operation(
            "accelerate_failed",
            appid=task.appid,
            name=task.name,
            src=str(backup),
            dst=str(cache_copy),
            result="failed",
            detail=f"{exc}；{detail}",
        )
        raise OperationFailed(f"加速失败：{exc}", detail=detail, rolled_back=rolled) from exc

    # 步骤 4：创建联接
    try:
        create_junction(game_path, cache_copy)
        assert_junction_points_to(game_path, cache_copy)
    except (JunctionError, OSError) as exc:
        rolled = _rollback_accelerate(state, task, game_path, backup, cache_copy, deletion_confirmer)
        detail = "已回滚，游戏仍在母盘原状" if rolled else "回滚未完成：请用修复向导处理"
        log_operation(
            "accelerate_failed",
            appid=task.appid,
            name=task.name,
            src=str(backup),
            dst=str(cache_copy),
            result="failed",
            detail=f"创建联接失败：{exc}；{detail}",
        )
        raise OperationFailed(f"创建目录联接失败：{exc}", detail=detail, rolled_back=rolled) from exc

    # 步骤 5：记录状态
    state.set_phase(
        task.appid,
        PHASE_ACCELERATED,
        cache_copy=str(cache_copy),
        mother_path=str(backup),
        junction=str(game_path),
        notes="",
    )
    log_operation(
        "accelerate_done",
        appid=task.appid,
        name=task.name,
        src=str(backup),
        dst=str(cache_copy),
        size_bytes=total.bytes,
        result="ok",
        detail=f"junction={game_path}",
    )
    return OperationResult(
        ok=True,
        action="accelerate",
        appid=task.appid,
        name=task.name,
        message=f"已加速：{game.name}（{human_size(total.bytes)}）",
        detail=result.detail,
        backup_path=str(backup),
        cache_copy=str(cache_copy),
        junction=str(game_path),
        files=total.files,
        bytes_copied=result.bytes_copied,
    )


def _rollback_accelerate(
    state: StateStore,
    task,
    game_path: Path,
    backup: Path,
    cache_copy: Path,
    deletion_confirmer,
) -> bool:
    """加速失败/取消后的回滚：删半成品副本（需确认）+ 母本改名回原位。"""
    rolled = True
    if cache_copy.exists():
        try:
            request = build_request(
                KIND_PARTIAL_COPY,
                cache_copy,
                str(cache_copy.parent),
                reason=f"加速未完成，清理半成品副本：{task.name}",
                reversible=False,
            )
            token = confirm_deletion(request, deletion_confirmer)
            safe_remove_tree(token)
        except DeletionNotConfirmed as exc:
            rolled = False
            task.notes = f"半成品副本未清理（用户未确认）：{cache_copy}"
            log.warning("回滚时用户拒绝删除半成品副本：%s", exc)
        except Exception as exc:  # noqa: BLE001
            rolled = False
            task.notes = f"清理半成品副本失败：{exc}"
            log.exception("清理半成品副本失败")
    if backup.is_dir() and not game_path.exists():
        try:
            os.rename(backup, game_path)
        except OSError as exc:
            rolled = False
            task.notes = f"母本改名回原位失败：{exc}"
            log.exception("母本改名回原位失败")
    state.set_phase(task.appid, PHASE_FAILED if not rolled else "")
    if not rolled:
        state.set_phase(task.appid, PHASE_FAILED, notes=task.notes)
    else:
        state.remove(task.appid)
    return rolled


# ------------------------------------------------------------------ 回写
def writeback(
    game,
    cfg: Config,
    *,
    store: StateStore | None = None,
    on_progress: Callable[[ProgressState], None] | None = None,
    extras_confirmer=None,
    engine=None,
) -> OperationResult:
    """把 SSD 副本的差异镜像回母盘，**保持加速状态**（回写独立按钮）。"""
    state = store or StateStore(cfg=cfg)
    _require(preflight_accelerated(game, cfg), "回写")

    task = state.get(game.appid) or build_task(game, cfg)
    cache_copy = Path(game.cache_copy_path)
    backup = Path(game.hdd_backup_path)
    game_path = Path(game.game_path)

    # 1) 先算清单，再请你确认（这一步不动物任何文件）
    extras, extras_bytes = plan_extras(cache_copy, backup)
    if extras:
        confirmer = extras_confirmer or DenyAllExtrasConfirmer()
        approved = False
        try:
            approved = bool(confirmer.confirm_extras(game.name, extras, extras_bytes))
        except Exception:  # noqa: BLE001
            log.exception("多余文件确认过程异常，按取消处理")
        log_operation(
            "writeback_extras",
            appid=task.appid,
            name=task.name,
            src=str(backup),
            size_bytes=extras_bytes,
            result="confirmed" if approved else "refused",
            detail=f"files={len(extras)} confirmer={getattr(confirmer, 'name', '?')}",
        )
        if not approved:
            raise OperationBlocked(
                f"未确认搬移母盘多余文件（{len(extras)} 个 / {human_size(extras_bytes)}），回写已中止，未改动任何文件",
                [f"待搬移：{name}" for name in extras[:10]],
            )

    state.set_phase(task.appid, PHASE_WRITING_BACK)
    log_operation(
        "writeback_start",
        appid=task.appid,
        name=task.name,
        src=str(cache_copy),
        dst=str(backup),
        result="running",
        detail=f"extras={len(extras)}",
    )

    # 2) 搬移多余文件到隔离区（移动，不是删除）
    item = None
    try:
        if extras:
            item = quarantine_extras(
                extras,
                backup,
                appid=task.appid,
                game_name=task.name or task.installdir,
                mother_drive=cfg.mother_drive,
            )
    except Exception as exc:  # noqa: BLE001
        state.set_phase(task.appid, PHASE_ACCELERATED, notes=f"搬移多余文件失败：{exc}")
        raise OperationFailed(f"搬移母盘多余文件失败，回写中止：{exc}", rolled_back=False) from exc

    # 3) 镜像复制（此时母盘已不多任何文件，全程无删除）
    try:
        active_engine = engine or detect_engine(cfg)
        result = active_engine.copy_tree(
            cache_copy,
            backup,
            mode=MODE_MIRROR,
            verify=cfg.fastcopy_verify,
            on_progress=on_progress,
            total=scan_tree(cache_copy),
        )
    except (CopyCancelled, CopyFailed) as exc:
        state.set_phase(task.appid, PHASE_ACCELERATED, notes=f"回写失败：{exc}")
        log_operation(
            "writeback_failed",
            appid=task.appid,
            name=task.name,
            src=str(cache_copy),
            dst=str(backup),
            result="failed",
            detail=str(exc),
        )
        raise OperationFailed(f"回写失败：{exc}", detail="母本可能处于半同步状态，可重新回写（镜像幂等）") from exc

    # 4) 严格校验
    verify = verify_trees(cache_copy, backup, allow_extra=False)
    if not verify.ok:
        state.set_phase(task.appid, PHASE_ACCELERATED, notes=f"回写校验未通过：{verify.detail}")
        log_operation(
            "writeback_failed",
            appid=task.appid,
            name=task.name,
            src=str(cache_copy),
            dst=str(backup),
            result="verify_failed",
            detail=verify.detail,
        )
        raise OperationFailed(
            f"回写校验未通过：{verify.detail}",
            detail="母本未被破坏，可重新回写；加速状态保持不变",
        )

    state.set_phase(
        task.appid,
        PHASE_ACCELERATED,
        last_writeback=time.time(),
        quarantine_item=item.item_dir if item else "",
        notes="",
    )
    log_operation(
        "writeback_done",
        appid=task.appid,
        name=task.name,
        src=str(cache_copy),
        dst=str(backup),
        result="ok",
        detail=f"extras={len(extras)} quarantine={item.item_dir if item else '-'}",
    )
    return OperationResult(
        ok=True,
        action="writeback",
        appid=task.appid,
        name=task.name,
        message=f"已回写母盘：{game.name}"
        + (f"（{len(extras)} 个多余文件已移入隔离区）" if extras else "（无需搬移多余文件）"),
        detail=result.detail,
        backup_path=str(backup),
        cache_copy=str(cache_copy),
        junction=str(game_path),
        quarantine_item=item.item_dir if item else "",
        files=result.files_copied,
        bytes_copied=result.bytes_copied,
    )


# ------------------------------------------------------------------ 释放
def release(
    game,
    cfg: Config,
    *,
    store: StateStore | None = None,
    on_progress: Callable[[ProgressState], None] | None = None,
    deletion_confirmer=None,
    extras_confirmer=None,
    engine=None,
) -> OperationResult:
    """回写 → 删联接 → 母本回归原位 → 删除 SSD 副本。"""
    state = store or StateStore(cfg=cfg)
    _require(preflight_accelerated(game, cfg), "释放")

    task = state.get(game.appid) or build_task(game, cfg)
    cache_copy = Path(game.cache_copy_path)
    backup = Path(game.hdd_backup_path)
    game_path = Path(game.game_path)
    cache_root = cfg.resolved_cache_dir()

    # 1) **先取删除凭据**：拒绝就地中止，此时什么都还没动
    token = None
    if cache_copy.exists():
        request = build_request(
            KIND_CACHE_COPY,
            cache_copy,
            str(cache_root),
            reason=f"释放 SSD 空间：删除 {game.name} 的缓存副本（游戏母本会保留在母盘）",
            reversible=False,
        )
        token = confirm_deletion(request, deletion_confirmer)

    # 2) 回写（内部会就"多余文件搬移"再问你一次）
    writeback_result = writeback(
        game,
        cfg,
        store=state,
        on_progress=on_progress,
        extras_confirmer=extras_confirmer,
        engine=engine,
    )

    state.set_phase(task.appid, PHASE_RELEASING)

    # 3) 删除联接（只摘联接本身）
    try:
        assert_junction_points_to(game_path, cache_copy)
        remove_junction(game_path)
    except (JunctionError, OSError) as exc:
        state.set_phase(task.appid, PHASE_FAILED, notes=f"删除联接失败：{exc}")
        raise OperationFailed(
            f"删除目录联接失败：{exc}",
            detail="回写已完成，母本是最新的；可重试释放或手动检查",
        ) from exc

    # 4) 母本改回原位置
    try:
        os.rename(backup, game_path)
    except OSError as exc:
        # 联接已删、母本还没回去：这是最需要谨慎的状态，尝试把联接建回来
        try:
            if not game_path.exists():
                create_junction(game_path, cache_copy)
        except Exception:  # noqa: BLE001
            log.exception("恢复联接失败")
        state.set_phase(task.appid, PHASE_FAILED, notes=f"母本改名回原位失败：{exc}")
        raise OperationFailed(
            f"母本改名回原位失败：{exc}",
            detail="母本仍在 .hdd_cache 下，请用修复向导处理；缓存副本未删除",
        ) from exc

    # 5) 删除缓存副本（用第 1 步拿到的凭据）
    freed = 0
    if token is not None:
        freed = safe_remove_tree(token)

    state.remove(task.appid)
    log_operation(
        "release_done",
        appid=task.appid,
        name=task.name,
        src=str(cache_copy),
        dst=str(game_path),
        size_bytes=freed,
        result="ok",
        detail=f"writeback={writeback_result.detail}",
    )
    return OperationResult(
        ok=True,
        action="release",
        appid=task.appid,
        name=task.name,
        message=f"已释放：{game.name} 回到母盘，加速盘释放 {human_size(freed)}",
        detail=writeback_result.detail,
        backup_path=str(backup),
        cache_copy=str(cache_copy),
        junction=str(game_path),
        quarantine_item=writeback_result.quarantine_item,
        bytes_copied=freed,
    )


__all__ = [
    "AutoExtrasConfirmer",
    "CheckResult",
    "DenyAllExtrasConfirmer",
    "ExtrasConfirmer",
    "OperationBlocked",
    "OperationFailed",
    "OperationResult",
    "accelerate",
    "preflight_accelerate",
    "preflight_accelerated",
    "release",
    "writeback",
]
