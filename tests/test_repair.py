"""异常恢复验证：把各类"中断现场"造出来，检查识别与修复是否正确。

覆盖：
  1. 加速中断（母本在 .hdd_cache、无联接）→ 识别 + 「继续加速」→ 变成已加速
  2. 加速中断 → 「回滚到机械盘」→ 母本回原位、状态清除
  3. 回写中断 → 「重新回写」→ 两侧一致，且仍是加速状态
  4. 联接损坏（指向不存在的目标）→ 「把母本放回原位」→ 联接摘除、母本归位
  5. 无主母本（.hdd_cache 里没有清单对应的备份）→ 「放回原位」
  6. 无主缓存副本 → 删除必须经过确认；无确认者时**必须拒绝**
  7. 干净状态下 analyze 不产生任何修复项

隔离布局：母盘 = 测试沙箱所在卷／加速盘 = 运行时挑出的另一个可写卷（都不是写死的盘符）。
运行： python tests/test_repair.py
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 日志重定向到测试沙箱（必须在导入应用模块之前设置）
os.environ["STEAMBOOST_LOG_DIR"] = str(Path(__file__).resolve().parent / "_repair_sandbox" / "logs")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from drive_picker import drive_of, prepare_cache_root  # noqa: E402

from config import HDD_CACHE_DIR_NAME, Config, trash_root  # noqa: E402
from copy_engine import verify_trees  # noqa: E402
from deletion_guard import DeletionNotConfirmed, DeletionRequest  # noqa: E402
from junction_utils import create_junction, is_junction, junction_target, remove_junction  # noqa: E402
from quarantine import list_items as quarantine_items, purge_item  # noqa: E402
from repair import (  # noqa: E402
    ACTION_DELETE_CACHE_COPY,
    ACTION_KEEP,
    ACTION_REAPPLY_WRITEBACK,
    ACTION_RESUME_ACCELERATE,
    ACTION_RESTORE_MOTHER,
    ACTION_ROLLBACK_TO_HDD,
    KIND_ACCELERATE_INTERRUPTED,
    KIND_JUNCTION_BROKEN,
    KIND_ORPHAN_BACKUP,
    KIND_ORPHAN_CACHE,
    KIND_WRITEBACK_INTERRUPTED,
    analyze,
    apply,
)
from state import PHASE_ACCELERATED, PHASE_ACCELERATING, PHASE_WRITING_BACK, StateStore, TaskState  # noqa: E402
from steam_scanner import scan  # noqa: E402

SANDBOX = Path(__file__).resolve().parent / "_repair_sandbox"
#: 母盘 = 沙箱所在卷；加速盘与缓存目录在 main() 里探测（都不写死盘符）
MOTHER_DRIVE = drive_of(SANDBOX)
CACHE_ROOT: Path | None = None
TEST_MARKER = "SteamBoostTest"
failures: list[str] = []

ACF = """\"AppState\"
{{
\t\"appid\"\t\t\"{appid}\"
\t\"name\"\t\t\"{name}\"
\t\"StateFlags\"\t\t\"4\"
\t\"installdir\"\t\t\"{installdir}\"
\t\"LastPlayed\"\t\t\"0\"
\t\"SizeOnDisk\"\t\t\"{size}\"
}}
"""


@dataclass
class ScriptedConfirmer:
    """测试用删除确认者。"""

    answers: list[bool]
    requests: list[DeletionRequest] = field(default_factory=list)
    name: str = "scripted"

    def confirm(self, request: DeletionRequest) -> bool:
        self.requests.append(request)
        return self.answers.pop(0) if self.answers else False


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f"  → {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def expect_raises(name: str, exc_type: type[BaseException], func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except exc_type as exc:
        check(name, True)
        return exc
    except BaseException as exc:  # noqa: BLE001
        check(name, False, f"抛出了 {type(exc).__name__}: {exc}")
        return None
    check(name, False, "没有抛出异常")
    return None


def make_game(lib: Path, appid: str, name: str, installdir: str) -> Path:
    """造一份游戏内容 + 清单，返回游戏目录。"""
    steamapps = lib / "steamapps"
    game_dir = steamapps / "common" / installdir
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "game.exe").write_bytes(b"E" * 2048)
    (game_dir / "data").mkdir(exist_ok=True)
    (game_dir / "data" / "pack.pak").write_bytes(b"P" * 8192)
    total = sum(p.stat().st_size for p in game_dir.rglob("*") if p.is_file())
    (steamapps / f"appmanifest_{appid}.acf").write_text(
        ACF.format(appid=appid, name=f"{name} {TEST_MARKER}", installdir=installdir, size=total),
        encoding="utf-8",
    )
    return game_dir


def make_lib(root: Path) -> Path:
    lib = root / "lib"
    (lib / "steamapps" / "common").mkdir(parents=True, exist_ok=True)
    (lib / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"' + str(lib).replace("\\", "\\\\")
        + '"\n\t\t"label"\t\t""\n\t}\n}\n',
        encoding="utf-8",
    )
    return lib


def make_config() -> Config:
    cfg = Config()
    cfg.mother_drive = MOTHER_DRIVE
    cfg.cache_drive = drive_of(str(CACHE_ROOT))
    cfg.cache_dir = str(CACHE_ROOT)
    return cfg


def state_for(game_path: Path, cache_copy: Path, backup: Path, appid: str, name: str, phase: str) -> TaskState:
    return TaskState(
        appid=appid, name=name, installdir=Path(game_path).name,
        library=str(Path(game_path).parent.parent.parent),
        mother_path=str(backup), cache_copy=str(cache_copy), junction=str(game_path), phase=phase,
    )


def main() -> int:
    global CACHE_ROOT

    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    SANDBOX.mkdir(parents=True)
    CACHE_ROOT = prepare_cache_root(MOTHER_DRIVE, f"{TEST_MARKER}CacheRepair")
    if CACHE_ROOT is None:
        print(
            f"[跳过] 本机只有 {MOTHER_DRIVE}: 一个可用卷，无法做跨盘异常恢复测试"
            "（本工具的前提就是母盘与加速盘是两个盘）。"
        )
        shutil.rmtree(SANDBOX, ignore_errors=True)
        return 0
    print(f"母盘 {MOTHER_DRIVE}:　加速盘 {drive_of(str(CACHE_ROOT))}:（缓存 {CACHE_ROOT}）")
    cfg = make_config()
    store = StateStore(SANDBOX / "state.json", cfg=cfg)
    approve = lambda: ScriptedConfirmer(answers=[True])  # noqa: E731

    # ---------------- 1) 加速中断 → 继续加速 ----------------
    lib_a = make_lib(SANDBOX / "a")
    game_a = make_game(lib_a, "900001", "中断游戏甲", "GameA")
    backup_a = lib_a / "steamapps" / "common" / HDD_CACHE_DIR_NAME / "GameA"
    backup_a.parent.mkdir(parents=True, exist_ok=True)
    os.rename(game_a, backup_a)
    cache_a = CACHE_ROOT / "900001_GameA"
    store.upsert(state_for(game_a, cache_a, backup_a, "900001", "中断游戏甲", PHASE_ACCELERATING))

    report = scan(cfg, state=store.to_dict(), steam_root=str(lib_a))
    cases = analyze(report, cfg, store)
    case_a = next((c for c in cases if c.kind == KIND_ACCELERATE_INTERRUPTED and c.appid == "900001"), None)
    check("识别出加速中断", case_a is not None, str([c.kind for c in cases]))
    if case_a is not None:
        keys = [a.key for a in case_a.actions]
        check("给出「继续加速」与「回滚」两个选项",
              ACTION_RESUME_ACCELERATE in keys and ACTION_ROLLBACK_TO_HDD in keys, str(keys))
        check("「继续加速」被标为推荐", case_a.action(ACTION_RESUME_ACCELERATE).recommended)
        message = apply(case_a, ACTION_RESUME_ACCELERATE, cfg, store)
        check("继续加速执行成功", "已继续加速" in message, message)
        check("原位置变成联接", is_junction(game_a))
        check("联接指向缓存副本", os.path.normcase(junction_target(game_a)) == os.path.normcase(str(cache_a)))
        check("缓存副本与母本一致", verify_trees(backup_a, cache_a, allow_extra=False).ok)
        check("状态更新为已加速", store.phase_of("900001") == PHASE_ACCELERATED, store.phase_of("900001"))

    # ---------------- 2) 加速中断 → 回滚 ----------------
    lib_b = make_lib(SANDBOX / "b")
    game_b = make_game(lib_b, "900002", "中断游戏乙", "GameB")
    backup_b = lib_b / "steamapps" / "common" / HDD_CACHE_DIR_NAME / "GameB"
    backup_b.parent.mkdir(parents=True, exist_ok=True)
    os.rename(game_b, backup_b)
    cache_b = CACHE_ROOT / "900002_GameB"
    store.upsert(state_for(game_b, cache_b, backup_b, "900002", "中断游戏乙", PHASE_ACCELERATING))

    report_b = scan(cfg, state=store.to_dict(), steam_root=str(lib_b))
    case_b = next((c for c in analyze(report_b, cfg, store) if c.appid == "900002"), None)
    check("识别出第二处加速中断", case_b is not None)
    if case_b is not None:
        message = apply(case_b, ACTION_ROLLBACK_TO_HDD, cfg, store, deletion_confirmer=approve())
        check("回滚执行成功", "已回滚" in message, message)
        check("母本回到原位置（真实目录）", game_b.is_dir() and not is_junction(game_b))
        check("暂存目录已清空", not backup_b.exists())
        check("状态记录已清除", store.get("900002") is None)

    # ---------------- 3) 回写中断 → 重新回写 ----------------
    lib_c = make_lib(SANDBOX / "c")
    game_c = make_game(lib_c, "900003", "回写中断游戏", "GameC")
    backup_c = lib_c / "steamapps" / "common" / HDD_CACHE_DIR_NAME / "GameC"
    backup_c.parent.mkdir(parents=True, exist_ok=True)
    os.rename(game_c, backup_c)
    cache_c = CACHE_ROOT / "900003_GameC"
    shutil.copytree(backup_c, cache_c)
    (cache_c / "data" / "pack.pak").write_bytes(b"P" * 16384)   # 模拟 Steam 在加速盘上更新了
    (cache_c / "dlc").mkdir()
    (cache_c / "dlc" / "new.pak").write_bytes(b"N" * 4096)
    os.utime(backup_c / "data" / "pack.pak", (1700000000, 1700000000))
    create_junction(game_c, cache_c)
    store.upsert(state_for(game_c, cache_c, backup_c, "900003", "回写中断游戏", PHASE_WRITING_BACK))

    report_c = scan(cfg, state=store.to_dict(), steam_root=str(lib_c))
    case_c = next((c for c in analyze(report_c, cfg, store) if c.kind == KIND_WRITEBACK_INTERRUPTED), None)
    check("识别出回写中断", case_c is not None, str([c.kind for c in analyze(report_c, cfg, store)]))
    if case_c is not None:
        check("给出「重新回写」选项", case_c.action(ACTION_REAPPLY_WRITEBACK) is not None)
        message = apply(case_c, ACTION_REAPPLY_WRITEBACK, cfg, store,
                        report=report_c, extras_confirmer=ScriptedExtras(answers=[True]))
        check("重新回写执行成功", "已重新回写" in message, message)
        check("母本与缓存副本一致", verify_trees(cache_c, backup_c, allow_extra=False).ok)
        check("新文件已同步到母本", (backup_c / "dlc" / "new.pak").exists())
        check("仍是加速状态", is_junction(game_c))
        check("状态回到已加速", store.phase_of("900003") == PHASE_ACCELERATED, store.phase_of("900003"))

    # ---------------- 4) 联接损坏 → 母本放回原位 ----------------
    lib_d = make_lib(SANDBOX / "d")
    game_d = make_game(lib_d, "900004", "联接损坏游戏", "GameD")
    backup_d = lib_d / "steamapps" / "common" / HDD_CACHE_DIR_NAME / "GameD"
    backup_d.parent.mkdir(parents=True, exist_ok=True)
    os.rename(game_d, backup_d)
    ghost = CACHE_ROOT / "900004_GameD"      # 故意建一个联接指向它，然后把它删掉（模拟用户手工删除缓存副本）
    ghost.mkdir()
    (ghost / "x.bin").write_bytes(b"x" * 16)
    create_junction(game_d, ghost)
    shutil.rmtree(ghost)
    store.upsert(state_for(game_d, ghost, backup_d, "900004", "联接损坏游戏", PHASE_ACCELERATED))

    report_d = scan(cfg, state=store.to_dict(), steam_root=str(lib_d))
    case_d = next((c for c in analyze(report_d, cfg, store) if c.kind == KIND_JUNCTION_BROKEN), None)
    check("识别出联接损坏", case_d is not None, str([c.kind for c in analyze(report_d, cfg, store)]))
    if case_d is not None:
        message = apply(case_d, ACTION_RESTORE_MOTHER, cfg, store, deletion_confirmer=approve())
        check("母本放回原位成功", "母本已回到原位" in message, message)
        check("原位置变成真实目录", game_d.is_dir() and not is_junction(game_d))
        check("暂存备份已消失", not backup_d.exists())
        check("状态记录已清除", store.get("900004") is None)

    # ---------------- 5) 无主母本 → 放回原位 ----------------
    lib_e = make_lib(SANDBOX / "e")
    orphan = lib_e / "steamapps" / "common" / HDD_CACHE_DIR_NAME / "GhostGame"
    orphan.mkdir(parents=True)
    (orphan / "left.bin").write_bytes(b"L" * 64)

    report_e = scan(cfg, state=store.to_dict(), steam_root=str(lib_e))
    case_e = next((c for c in analyze(report_e, cfg, store) if c.kind == KIND_ORPHAN_BACKUP), None)
    check("识别出无主母本", case_e is not None, str([c.kind for c in analyze(report_e, cfg, store)]))
    if case_e is not None:
        message = apply(case_e, ACTION_RESTORE_MOTHER, cfg, store, deletion_confirmer=approve())
        check("无主母本放回原位成功", "母本已回到原位" in message, message)
        check("原位置出现该目录", (lib_e / "steamapps" / "common" / "GhostGame").is_dir())

    # ---------------- 6) 无主缓存副本 → 删除必须确认 ----------------
    ghost_copy = CACHE_ROOT / "900099_GhostCopy"
    ghost_copy.mkdir(parents=True)
    (ghost_copy / "junk.bin").write_bytes(b"J" * 1024)

    report_f = scan(cfg, state=store.to_dict(), steam_root=str(lib_e))
    case_f = next((c for c in analyze(report_f, cfg, store) if c.kind == KIND_ORPHAN_CACHE), None)
    check("识别出无主缓存副本", case_f is not None, str([c.kind for c in analyze(report_f, cfg, store)]))
    if case_f is not None:
        check("默认选项是保持不动", case_f.action(ACTION_KEEP).recommended)
        expect_raises(
            "无确认者时拒绝删除无主副本",
            DeletionNotConfirmed,
            apply, case_f, ACTION_DELETE_CACHE_COPY, cfg, store,
        )
        check("被拒后副本仍在", ghost_copy.is_dir())
        message = apply(case_f, ACTION_DELETE_CACHE_COPY, cfg, store, deletion_confirmer=approve())
        check("确认后删除成功", "已删除无主副本" in message, message)
        check("副本已消失", not ghost_copy.exists())

    # ---------------- 7) 干净状态下不产生修复项 ----------------
    lib_clean = make_lib(SANDBOX / "clean")
    make_game(lib_clean, "900010", "正常游戏", "CleanGame")
    report_clean = scan(cfg, state=store.to_dict(), steam_root=str(lib_clean))
    case_clean = analyze(report_clean, cfg, store)
    check("干净状态没有修复项", case_clean == [], str([c.kind for c in case_clean]))

    # ---------------- 收尾 ----------------
    for lib in SANDBOX.glob("lib*"):
        for path in lib.rglob("*"):
            if path.is_dir() and is_junction(path):
                remove_junction(path)
    from logger import close_loggers

    drive = MOTHER_DRIVE
    trash = trash_root(drive)
    if trash.is_dir():
        for item in quarantine_items(drive):
            if TEST_MARKER in item.name:
                purge_item(item.item_dir, drive, ScriptedConfirmer(answers=[True]))
    close_loggers()
    shutil.rmtree(SANDBOX, ignore_errors=True)
    shutil.rmtree(CACHE_ROOT, ignore_errors=True)
    check("沙箱已清理", not SANDBOX.exists())
    check("测试缓存目录已清理", not CACHE_ROOT.exists())

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        return 1
    print("全部通过")
    return 0


@dataclass
class ScriptedExtras:
    """测试用多余文件确认者。"""

    answers: list[bool]
    name: str = "scripted-extras"

    def confirm_extras(self, game_name: str, extras: list[str], total_bytes: int) -> bool:
        return self.answers.pop(0) if self.answers else False


if __name__ == "__main__":
    sys.exit(main())
