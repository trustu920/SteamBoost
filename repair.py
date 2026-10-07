"""异常恢复：把"中断的加速/回写/释放"翻译成人能看懂的修复选项。

为什么需要它
------------
加速与释放都是多步操作（改名 → 复制 → 校验 → 建联接 → 删副本…），
中途断电、蓝屏、强杀进程都会停在中间状态。程序不能猜，也不能自动"清理"，
必须：**识别出精确的现场状态 → 给出有限几个安全选项 → 用户选了才执行**。

本模块只做两件事
----------------
* :func:`analyze` —— 读 :mod:`state` 与 :mod:`steam_scanner` 的结果，
  产出若干 :class:`RepairCase`（每个都带可选动作与推荐项）；
* :func:`apply` —— 执行用户选定的动作。所有删除仍走 :mod:`deletion_guard`，
  没有确认者一律拒绝，绝不"顺手清理"。

设计原则：**任何动作都不能让数据变少**（除了用户明确确认删除的缓存副本）。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import HDD_CACHE_DIR_NAME, Config, human_size
from copy_engine import MODE_DIFF, MODE_MIRROR, detect_engine, scan_tree, verify_trees
from deletion_guard import (
    KIND_CACHE_COPY,
    KIND_QUARANTINE,
    DeletionNotConfirmed,
    build_request,
    confirm_deletion,
    safe_remove_tree,
)
from junction_utils import (
    JunctionError,
    create_junction,
    is_junction,
    junction_target,
    remove_junction,
)
from logger import log_operation, setup_logger
from state import (
    PHASE_ACCELERATED,
    PHASE_ACCELERATING,
    PHASE_FAILED,
    PHASE_RELEASING,
    PHASE_WRITING_BACK,
    StateStore,
    TaskState,
)

log = setup_logger("steamboot.repair")

# ---------------------------------------------------------------- 现场类别
KIND_ACCELERATE_INTERRUPTED = "accelerate_interrupted"   # 母本在 .hdd_cache，原位置还没有联接
KIND_WRITEBACK_INTERRUPTED = "writeback_interrupted"     # 回写做了一半
KIND_RELEASE_INTERRUPTED = "release_interrupted"         # 释放做了一半
KIND_JUNCTION_BROKEN = "junction_broken"                 # 联接指向不存在的目标
KIND_ORPHAN_BACKUP = "orphan_backup"                     # .hdd_cache 里的无主母本
KIND_ORPHAN_CACHE = "orphan_cache_copy"                  # 缓存根里的无主副本

# ---------------------------------------------------------------- 可选动作
ACTION_RESUME_ACCELERATE = "resume_accelerate"   # 继续加速（复制+校验+建联接）
ACTION_ROLLBACK_TO_HDD = "rollback_to_hdd"       # 回滚：母本改名回原位
ACTION_REAPPLY_WRITEBACK = "reapply_writeback"   # 再回写一次（幂等）
ACTION_RESTORE_MOTHER = "restore_mother"         # 摘掉坏联接 + 母本回原位
ACTION_FINISH_RELEASE = "finish_release"         # 完成释放（含删除缓存副本，需确认）
ACTION_DELETE_CACHE_COPY = "delete_cache_copy"   # 删除无主缓存副本（需确认）
ACTION_KEEP = "keep"                             # 保持现状，仅记录


class RepairFailed(RuntimeError):
    """修复动作执行失败。"""


@dataclass
class RepairAction:
    """一个可执行的修复选项。"""

    key: str
    label: str
    detail: str = ""
    destructive: bool = False
    recommended: bool = False


@dataclass
class RepairCase:
    """一处需要用户决定的现场。"""

    kind: str
    title: str
    detail: str
    appid: str = ""
    game_name: str = ""
    paths: dict[str, str] = field(default_factory=dict)
    actions: list[RepairAction] = field(default_factory=list)

    def action(self, key: str) -> RepairAction | None:
        return next((item for item in self.actions if item.key == key), None)


# ---------------------------------------------------------------- 分析
def analyze(report, cfg: Config, store: StateStore) -> list[RepairCase]:
    """扫描现场，产出需要用户决定的修复项（按严重程度排序）。"""
    cases: list[RepairCase] = []
    seen_appids: set[str] = set()

    # 1) 先看状态文件里"没走完"的任务
    for task in store.tasks.values():
        case = _case_from_task(task, cfg)
        if case is not None:
            cases.append(case)
            seen_appids.add(task.appid)

    # 2) 再看扫描器识别出的异常状态（可能没有状态记录，例如上次运行时状态文件丢了）
    for game in getattr(report, "games", []):
        if game.appid in seen_appids:
            continue
        from steam_scanner import ST_ACCELERATING, ST_UNKNOWN, ST_WRITING_BACK

        if game.status == ST_ACCELERATING:
            cases.append(_case_accelerate_interrupted(game, cfg))
            seen_appids.add(game.appid)
        elif game.status == ST_WRITING_BACK:
            cases.append(_case_writeback_interrupted(game, store.get(game.appid), cfg))
            seen_appids.add(game.appid)
        elif game.status == ST_UNKNOWN and is_junction(game.game_path):
            cases.append(_case_junction_broken(game))
            seen_appids.add(game.appid)

    # 3) .hdd_cache 里的无主母本（扫描器已发现）
    for anomaly in getattr(report, "anomalies", []):
        if anomaly.kind == "orphan_backup":
            cases.append(_case_orphan_backup(anomaly.path))

    # 4) 缓存根里的无主副本
    cases.extend(_find_orphan_cache_copies(report, cfg, store))

    order = {
        KIND_RELEASE_INTERRUPTED: 0,
        KIND_JUNCTION_BROKEN: 1,
        KIND_ACCELERATE_INTERRUPTED: 2,
        KIND_WRITEBACK_INTERRUPTED: 3,
        KIND_ORPHAN_BACKUP: 4,
        KIND_ORPHAN_CACHE: 5,
    }
    cases.sort(key=lambda item: order.get(item.kind, 9))
    return cases


def _case_from_task(task: TaskState, cfg: Config) -> RepairCase | None:
    """按状态文件里的阶段判断现场。"""
    game_path = Path(task.junction) if task.junction else None
    backup = Path(task.mother_path) if task.mother_path else None
    cache_copy = Path(task.cache_copy) if task.cache_copy else None

    junction_ok = bool(game_path and is_junction(game_path))
    game_real = bool(game_path and game_path.is_dir() and not junction_ok)
    backup_ok = bool(backup and backup.is_dir())
    cache_ok = bool(cache_copy and cache_copy.is_dir())

    if task.phase in (PHASE_ACCELERATING, PHASE_FAILED) and backup_ok and not junction_ok and not game_real:
        case = RepairCase(
            kind=KIND_ACCELERATE_INTERRUPTED,
            title=f"加速未完成：{task.name or task.appid}",
            detail=(
                "游戏母本还停在暂存目录 .hdd_cache 里，原位置没有联接，"
                "Steam 现在看不到这个游戏。\n"
                f"母本：{task.mother_path}\n缓存副本：{'已存在' if cache_ok else '不存在或未完成'}"
            ),
            appid=task.appid,
            game_name=task.name,
            paths={
                "game_path": task.junction,
                "backup": task.mother_path,
                "cache_copy": task.cache_copy,
            },
        )
        case.actions = [
            RepairAction(
                ACTION_RESUME_ACCELERATE, "继续加速（推荐）",
                "把母本复制到加速盘并建立联接，直接进入已加速状态",
                recommended=True,
            ),
            RepairAction(
                ACTION_ROLLBACK_TO_HDD, "回滚到机械盘",
                "把母本改回原位，游戏回到未加速状态（若已有缓存副本会先请你确认删除）",
            ),
        ]
        return case

    if task.phase == PHASE_WRITING_BACK and junction_ok:
        return _case_writeback_interrupted(None, task, cfg)

    if task.phase == PHASE_RELEASING:
        case = RepairCase(
            kind=KIND_RELEASE_INTERRUPTED,
            title=f"释放未完成：{task.name or task.appid}",
            detail=(
                "释放流程没走完。母本可能在暂存目录、也可能已回到原位。\n"
                f"联接：{'存在' if junction_ok else '不存在'}\n"
                f"母本暂存：{'存在' if backup_ok else '不存在'}\n"
                f"缓存副本：{'存在' if cache_ok else '不存在'}"
            ),
            appid=task.appid,
            game_name=task.name,
            paths={
                "game_path": task.junction,
                "backup": task.mother_path,
                "cache_copy": task.cache_copy,
            },
        )
        case.actions = []
        if backup_ok:
            case.actions.append(
                RepairAction(
                    ACTION_FINISH_RELEASE, "完成释放（推荐）",
                    "摘掉联接 → 母本回到原位 → 删除缓存副本（删除前会请你确认）",
                    destructive=True, recommended=True,
                )
            )
            case.actions.append(
                RepairAction(
                    ACTION_ROLLBACK_TO_HDD, "只把母本放回原位",
                    "不删缓存副本，母本回到游戏目录（缓存副本会变成无主文件，稍后可在本向导里清理）",
                )
            )
        case.actions.append(RepairAction(ACTION_KEEP, "暂不处理", "保持现状，下次启动再提醒"))
        return case
    return None


def _case_accelerate_interrupted(game, cfg: Config) -> RepairCase:
    case = RepairCase(
        kind=KIND_ACCELERATE_INTERRUPTED,
        title=f"加速未完成：{game.name}",
        detail=(
            "原位置没有游戏目录，母本停在 .hdd_cache 暂存区。\n"
            f"母本：{game.hdd_backup_path}\n缓存副本：{game.cache_copy_path}"
        ),
        appid=game.appid,
        game_name=game.name,
        paths={
            "game_path": game.game_path,
            "backup": game.hdd_backup_path,
            "cache_copy": game.cache_copy_path,
        },
    )
    case.actions = [
        RepairAction(ACTION_RESUME_ACCELERATE, "继续加速（推荐）",
                     "复制到加速盘并建立联接", recommended=True),
        RepairAction(ACTION_ROLLBACK_TO_HDD, "回滚到机械盘", "母本改回原位"),
    ]
    return case


def _case_writeback_interrupted(game, task: TaskState | None, cfg: Config) -> RepairCase:
    name = game.name if game is not None else (task.name if task else "")
    appid = game.appid if game is not None else (task.appid if task else "")
    cache_copy = game.cache_copy_path if game is not None else (task.cache_copy if task else "")
    backup = game.hdd_backup_path if game is not None else (task.mother_path if task else "")
    case = RepairCase(
        kind=KIND_WRITEBACK_INTERRUPTED,
        title=f"回写未完成：{name}",
        detail=(
            "上次回写中途停了。回写是镜像操作且幂等，重新跑一次即可收敛；\n"
            "期间游戏一直处于加速状态，不受影响。\n"
            f"缓存副本：{cache_copy}\n母本：{backup}"
        ),
        appid=appid,
        game_name=name,
        paths={
            "game_path": game.game_path if game is not None else (task.junction if task else ""),
            "backup": backup,
            "cache_copy": cache_copy,
        },
    )
    case.actions = [
        RepairAction(ACTION_REAPPLY_WRITEBACK, "重新回写（推荐）",
                     "把加速盘的差异重新镜像回母本，完成后仍是加速状态", recommended=True),
        RepairAction(ACTION_KEEP, "保持现状", "母本暂时不是最新，下次回写时再同步"),
    ]
    return case


def _case_junction_broken(game) -> RepairCase:
    target = junction_target(game.game_path)
    target_exists = bool(target) and Path(target).is_dir()
    backup_exists = bool(game.hdd_backup_path) and Path(game.hdd_backup_path).is_dir()
    case = RepairCase(
        kind=KIND_JUNCTION_BROKEN,
        title=f"联接异常：{game.name}",
        detail=(
            f"原位置是目录联接，但它指向的目标{'存在' if target_exists else '**不存在**'}：{target}\n"
            f"母本备份：{'存在' if backup_exists else '不存在'}"
        ),
        appid=game.appid,
        game_name=game.name,
        paths={
            "game_path": game.game_path,
            "backup": game.hdd_backup_path,
            "cache_copy": game.cache_copy_path,
        },
    )
    if backup_exists:
        case.actions.append(
            RepairAction(
                ACTION_RESTORE_MOTHER, "把母本放回原位（推荐）",
                "摘掉失效的联接，母本从 .hdd_cache 回到游戏目录；"
                "若还有残留的缓存副本会先请你确认删除",
                destructive=True, recommended=True,
            )
        )
    case.actions.append(RepairAction(ACTION_KEEP, "暂不处理", "保持现状"))
    return case


def _case_orphan_backup(backup_path: str) -> RepairCase:
    backup = Path(backup_path)
    installdir = backup.name
    common = backup.parent.parent
    game_path = common / installdir
    return RepairCase(
        kind=KIND_ORPHAN_BACKUP,
        title=f"发现无主母本：{installdir}",
        detail=(
            "暂存目录里有一份母本，但它没有对应的 Steam 清单，"
            "可能是上次操作中断、或游戏已被 Steam 卸载。\n"
            f"母本：{backup_path}\n原位置：{game_path}（{'已存在，请勿覆盖' if game_path.exists() else '不存在'}）"
        ),
        game_name=installdir,
        paths={"backup": str(backup), "game_path": str(game_path)},
        actions=[
            RepairAction(ACTION_RESTORE_MOTHER, "放回原位", "把这份母本改名为游戏目录"),
            RepairAction(ACTION_KEEP, "保持不动（推荐）", "先留着，不确认内容前不做任何改动", recommended=True),
        ],
    )


def _find_orphan_cache_copies(report, cfg: Config, store: StateStore) -> list[RepairCase]:
    """找出缓存根下既没有状态记录、也没有联接指向的副本目录。"""
    root = cfg.resolved_cache_dir()
    if not root.is_dir():
        return []
    referenced: set[str] = set()
    for task in store.tasks.values():
        if task.cache_copy:
            referenced.add(os.path.normcase(os.path.abspath(task.cache_copy)))
    for game in getattr(report, "games", []):
        target = junction_target(game.game_path) if is_junction(game.game_path) else ""
        if target:
            referenced.add(os.path.normcase(os.path.abspath(target)))

    cases: list[RepairCase] = []
    try:
        children = [path for path in root.iterdir() if path.is_dir()]
    except OSError:
        return []
    for child in children:
        if os.path.normcase(os.path.abspath(str(child))) in referenced:
            continue
        if child.name.startswith("."):
            continue
        stats = scan_tree(child)
        cases.append(
            RepairCase(
                kind=KIND_ORPHAN_CACHE,
                title=f"加速盘上有无主副本：{child.name}",
                detail=(
                    "这个缓存副本没有任何游戏在用它（没有联接指向它），"
                    "可能是释放流程中断留下的。\n"
                    f"路径：{child}\n内容：{stats.files} 个文件 / {human_size(stats.bytes)}"
                ),
                paths={"cache_copy": str(child)},
                actions=[
                    RepairAction(
                        ACTION_DELETE_CACHE_COPY, "删除这个副本",
                        "释放它占用的加速盘空间（删除前会弹窗让你核对确切路径）",
                        destructive=True,
                    ),
                    RepairAction(ACTION_KEEP, "保持不动（推荐）", "先留着，确认没有游戏需要它再说", recommended=True),
                ],
            )
        )
    return cases


# ---------------------------------------------------------------- 执行
def optional_path(case: RepairCase, key: str) -> Path | None:
    """取可选路径。

    注意：``Path("")`` 等于当前工作目录，会被 ``is_dir()`` 判为真——
    如果直接拿它去当"要删除的缓存副本"，后果不堪设想。所以这里统一返回 None。
    """
    raw = (case.paths.get(key) or "").strip()
    return Path(raw) if raw else None


def apply(
    case: RepairCase,
    action_key: str,
    cfg: Config,
    store: StateStore,
    *,
    report=None,
    deletion_confirmer=None,
    extras_confirmer=None,
    engine=None,
) -> str:
    """执行用户选定的修复动作，返回一句结果说明。

    ``report`` 传最近一次扫描结果可避免重复扫描（界面里已经有现成的）。
    """
    action = case.action(action_key)
    if action is None:
        raise RepairFailed(f"未知的修复动作：{action_key}")

    log_operation("repair_start", appid=case.appid, name=case.game_name,
                  src=case.paths.get("backup", ""), result="running",
                  detail=f"kind={case.kind} action={action_key}")

    if action_key == ACTION_KEEP:
        return "已保留现状，未做任何改动"

    if action_key == ACTION_RESUME_ACCELERATE:
        return _resume_accelerate(case, cfg, store, engine)

    if action_key == ACTION_ROLLBACK_TO_HDD:
        return _rollback_to_hdd(case, cfg, store, deletion_confirmer)

    if action_key == ACTION_REAPPLY_WRITEBACK:
        return _reapply_writeback(case, cfg, store, extras_confirmer, report)

    if action_key == ACTION_RESTORE_MOTHER:
        return _restore_mother(case, cfg, store, deletion_confirmer)

    if action_key == ACTION_FINISH_RELEASE:
        return _finish_release(case, cfg, store, deletion_confirmer, extras_confirmer, report)

    if action_key == ACTION_DELETE_CACHE_COPY:
        return _delete_cache_copy(case, cfg, deletion_confirmer)

    raise RepairFailed(f"动作尚未实现：{action_key}")


def _resume_accelerate(case: RepairCase, cfg: Config, store: StateStore, engine) -> str:
    """继续加速：母本 → 缓存副本（复制+校验）→ 建联接。"""
    backup = Path(case.paths["backup"])
    cache_copy = Path(case.paths["cache_copy"])
    game_path = Path(case.paths["game_path"])
    if not backup.is_dir():
        raise RepairFailed(f"母本不存在，无法继续加速：{backup}")
    if game_path.exists() or is_junction(game_path):
        raise RepairFailed(f"原位置已被占用，无法建立联接：{game_path}")

    total = scan_tree(backup)
    active_engine = engine or detect_engine(cfg)
    active_engine.copy_tree(backup, cache_copy, mode=MODE_DIFF, total=total)
    verify = verify_trees(backup, cache_copy, allow_extra=False)
    if not verify.ok:
        raise RepairFailed(f"复制后校验未通过：{verify.detail}（母本未动，可重试）")
    create_junction(game_path, cache_copy)
    store.set_phase(case.appid, PHASE_ACCELERATED, notes="修复向导：继续加速完成")
    log_operation("repair_done", appid=case.appid, name=case.game_name,
                  src=str(backup), dst=str(cache_copy), size_bytes=total.bytes,
                  result="ok", detail="action=resume_accelerate")
    return f"已继续加速：{case.game_name}（{human_size(total.bytes)}），现在处于已加速状态"


def _rollback_to_hdd(case: RepairCase, cfg: Config, store: StateStore, deletion_confirmer) -> str:
    """回滚：母本改回原位；若已有缓存副本，先请用户确认是否删除。"""
    backup = Path(case.paths["backup"])
    game_path = Path(case.paths["game_path"])
    cache_copy = optional_path(case, "cache_copy")

    if is_junction(game_path):
        remove_junction(game_path)
    if not backup.is_dir():
        raise RepairFailed(f"母本不存在，无法回滚：{backup}")
    if game_path.exists():
        raise RepairFailed(f"原位置已存在目录，拒绝覆盖：{game_path}")
    os.rename(backup, game_path)

    freed = 0
    kept = ""
    if cache_copy is not None and cache_copy.is_dir():
        try:
            request = build_request(
                KIND_CACHE_COPY, cache_copy, str(cfg.resolved_cache_dir()),
                reason=f"回滚时清理残留的缓存副本：{case.game_name}",
            )
            token = confirm_deletion(request, deletion_confirmer)
            freed = safe_remove_tree(token)
        except DeletionNotConfirmed:
            kept = "（残留的缓存副本已保留，因为未确认删除）"

    store.remove(case.appid)
    log_operation("repair_done", appid=case.appid, name=case.game_name,
                  src=str(game_path), size_bytes=freed, result="ok",
                  detail="action=rollback_to_hdd")
    return f"已回滚：{case.game_name} 回到机械盘" + (f"，并释放 {human_size(freed)}" if freed else "") + kept


def _reapply_writeback(case: RepairCase, cfg: Config, store: StateStore,
                       extras_confirmer, report=None) -> str:
    """重新回写（镜像，幂等）。"""
    from operations import writeback  # 延迟导入：避免与 operations 循环依赖
    from steam_scanner import scan

    active_report = report if report is not None else scan(cfg, state=store.to_dict())
    matches = active_report.find(case.appid)
    if not matches:
        raise RepairFailed(f"扫描不到 appid {case.appid} 的清单，无法自动回写")
    result = writeback(matches[0], cfg, store=store, extras_confirmer=extras_confirmer)
    log_operation("repair_done", appid=case.appid, name=case.game_name,
                  result="ok", detail="action=reapply_writeback")
    return f"已重新回写：{case.game_name}（{result.detail}）"


def _restore_mother(case: RepairCase, cfg: Config, store: StateStore, deletion_confirmer) -> str:
    """摘掉失效联接 + 母本回原位（可选清理残留缓存副本）。"""
    backup = Path(case.paths["backup"])
    game_path = Path(case.paths["game_path"])
    cache_copy = optional_path(case, "cache_copy")

    if not backup.is_dir():
        raise RepairFailed(f"母本不存在：{backup}")
    if is_junction(game_path):
        remove_junction(game_path)
    if game_path.exists():
        raise RepairFailed(f"原位置已被占用，拒绝覆盖：{game_path}")
    os.rename(backup, game_path)

    freed = 0
    kept = ""
    # 只有当缓存副本确实存在、且不再被任何联接指向时，才提议删除
    if cache_copy is not None and cache_copy.is_dir():
        try:
            request = build_request(
                KIND_CACHE_COPY, cache_copy, str(cfg.resolved_cache_dir()),
                reason=f"恢复后清理残留的缓存副本：{case.game_name}",
            )
            token = confirm_deletion(request, deletion_confirmer)
            freed = safe_remove_tree(token)
        except DeletionNotConfirmed:
            kept = "（残留的缓存副本已保留，因为未确认删除）"

    if case.appid:
        store.remove(case.appid)
    log_operation("repair_done", appid=case.appid, name=case.game_name,
                  src=str(game_path), size_bytes=freed, result="ok", detail="action=restore_mother")
    return f"母本已回到原位：{game_path}" + (f"，并释放 {human_size(freed)}" if freed else "") + kept


def _finish_release(case: RepairCase, cfg: Config, store: StateStore,
                    deletion_confirmer, extras_confirmer, report=None) -> str:
    """完成一次中断的释放。"""
    from operations import release  # 延迟导入
    from steam_scanner import scan

    active_report = report if report is not None else scan(cfg, state=store.to_dict())
    matches = active_report.find(case.appid)
    if not matches:
        raise RepairFailed(f"扫描不到 appid {case.appid} 的清单，无法自动完成释放")
    result = release(matches[0], cfg, store=store,
                     deletion_confirmer=deletion_confirmer, extras_confirmer=extras_confirmer)
    log_operation("repair_done", appid=case.appid, name=case.game_name,
                  result="ok", detail="action=finish_release")
    return result.message


def _delete_cache_copy(case: RepairCase, cfg: Config, deletion_confirmer) -> str:
    """删除无主的缓存副本（走删除闸门，必须有人确认）。"""
    cache_copy = Path(case.paths["cache_copy"])
    request = build_request(
        KIND_CACHE_COPY, cache_copy, str(cfg.resolved_cache_dir()),
        reason="修复向导：删除无主的缓存副本",
    )
    token = confirm_deletion(request, deletion_confirmer)
    freed = safe_remove_tree(token)
    log_operation("repair_done", src=str(cache_copy), size_bytes=freed,
                  result="ok", detail="action=delete_cache_copy")
    return f"已删除无主副本，释放 {human_size(freed)}"


__all__ = [
    "ACTION_DELETE_CACHE_COPY",
    "ACTION_FINISH_RELEASE",
    "ACTION_KEEP",
    "ACTION_REAPPLY_WRITEBACK",
    "ACTION_RESUME_ACCELERATE",
    "ACTION_RESTORE_MOTHER",
    "ACTION_ROLLBACK_TO_HDD",
    "KIND_ACCELERATE_INTERRUPTED",
    "KIND_JUNCTION_BROKEN",
    "KIND_ORPHAN_BACKUP",
    "KIND_ORPHAN_CACHE",
    "KIND_RELEASE_INTERRUPTED",
    "KIND_WRITEBACK_INTERRUPTED",
    "RepairAction",
    "RepairCase",
    "RepairFailed",
    "analyze",
    "apply",
]
