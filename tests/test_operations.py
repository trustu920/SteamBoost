"""阶段 3 端到端验证：加速 → 回写 → 释放，全部在隔离环境里跑。

隔离布局（刻意让母盘与加速盘**不同盘**，以符合"两个盘"的设计）：
  母盘 = 测试沙箱所在的卷（tests\\_ops_sandbox\\lib\\steamapps\\…，假 Steam 库、假游戏）
  加速盘 = 运行时挑出的另一个可写本地卷（测试自建缓存目录，跑完清掉）

  盘符一律**运行时探测**（见 drive_picker.py），不写死任何盘符。

覆盖的真实风险点：
  * 显示名与安装目录名不同（Sample Game Deluxe / SampleGameDir）时路径必须按 installdir 解析；
  * 加速：母本改名 → 复制 → 严格校验 → 建联接 → 状态记录；
  * 回滚：复制失败时删半成品副本（需确认）、母本改回原位；用户拒绝删除则保留副本并标记待修复；
  * 回写：多余文件确认被拒绝 → 中止且**一个文件都不动**；确认 → 搬入隔离区 → 镜像 → 严格校验；
  * 回写后**仍然是加速状态**（联接还在）；
  * 释放：删除缓存副本的确认被拒绝 → 中止且什么都不动；确认 → 删联接 → 母本回原位 → 删副本；
  * 全程审计日志留痕。

运行： python tests/test_operations.py
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 日志重定向到测试沙箱（必须在导入应用模块之前设置，避免污染真实审计日志）
os.environ["STEAMBOOST_LOG_DIR"] = str(Path(__file__).resolve().parent / "_ops_sandbox" / "logs")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from drive_picker import drive_of, prepare_cache_root  # noqa: E402

from config import Config, app_data_dir, trash_root  # noqa: E402
from copy_engine import CopyFailed, verify_trees  # noqa: E402
from deletion_guard import ConfirmedDeletion, DeletionNotConfirmed, DeletionRequest  # noqa: E402
from junction_utils import is_junction, junction_target  # noqa: E402
from operations import (  # noqa: E402
    OperationBlocked,
    OperationFailed,
    accelerate,
    preflight_accelerate,
    release,
    writeback,
)
from quarantine import list_items, purge_item  # noqa: E402
from state import PHASE_ACCELERATED, StateStore  # noqa: E402
from steam_scanner import ST_ACCELERATED, ST_ON_HDD, scan  # noqa: E402

SANDBOX = Path(__file__).resolve().parent / "_ops_sandbox"
#: 母盘 = 沙箱所在卷；加速盘在 main() 里探测，缓存目录随之确定（都不写死盘符）
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
class ScriptedDeletionConfirmer:
    """测试用删除确认者：按脚本回答并记录请求。"""

    answers: list[bool]
    requests: list[DeletionRequest] = field(default_factory=list)
    name: str = "scripted-deletion"

    def confirm(self, request: DeletionRequest) -> bool:
        self.requests.append(request)
        return self.answers.pop(0) if self.answers else False


@dataclass
class ScriptedExtrasConfirmer:
    """测试用多余文件确认者。"""

    answers: list[bool]
    requests: list[tuple[str, list[str], int]] = field(default_factory=list)
    name: str = "scripted-extras"

    def confirm_extras(self, game_name: str, extras: list[str], total_bytes: int) -> bool:
        self.requests.append((game_name, list(extras), total_bytes))
        return self.answers.pop(0) if self.answers else False


class FailingEngine:
    """故意失败的引擎：制造"复制中断并留下半成品"的状态，用来验证回滚。"""

    name = "failing-engine"

    def __init__(self, partial_files: int = 2) -> None:
        self.partial_files = partial_files

    def copy_tree(self, src, dst, **kwargs):
        dst = Path(dst)
        dst.mkdir(parents=True, exist_ok=True)
        for index in range(self.partial_files):
            (dst / f"partial{index}.bin").write_bytes(b"p" * 256)
        raise CopyFailed("模拟复制中断", detail="测试用")


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


def build_library(appid: str = "777777") -> tuple[Path, Path]:
    """造一个假 Steam 库；显示名与安装目录名刻意不同。"""
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    lib = SANDBOX / "lib"
    steamapps = lib / "steamapps"
    game_dir = steamapps / "common" / "SampleGameDir"
    game_dir.mkdir(parents=True)
    (game_dir / "game.exe").write_bytes(b"E" * 4096)
    (game_dir / "data").mkdir()
    (game_dir / "data" / "pack0.pak").write_bytes(b"P" * 65536)
    (game_dir / "data" / "pack1.pak").write_bytes(b"Q" * 32768)
    total = sum(p.stat().st_size for p in game_dir.rglob("*") if p.is_file())
    # 显示名里带上测试标记：隔离项目录名由游戏名派生，收尾时据此只清理本测试的产物
    (steamapps / f"appmanifest_{appid}.acf").write_text(
        ACF.format(
            appid=appid,
            name=f"Sample Game Deluxe {TEST_MARKER}",
            installdir="SampleGameDir",
            size=total,
        ),
        encoding="utf-8",
    )
    (steamapps / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"'
        + str(lib).replace("\\", "\\\\")
        + '"\n\t\t"label"\t\t""\n\t\t"apps"\n\t\t{\n\t\t\t"'
        + appid
        + '"\t\t"'
        + str(total)
        + '"\n\t\t}\n\t}\n}\n',
        encoding="utf-8",
    )
    return lib, game_dir


def make_config(lib: Path) -> Config:
    cfg = Config()
    cfg.mother_drive = MOTHER_DRIVE
    cfg.cache_drive = drive_of(str(CACHE_ROOT))
    cfg.cache_dir = str(CACHE_ROOT)
    cfg.copy_engine = "fastcopy"
    return cfg


def path_of(record, which: str) -> Path:
    return Path(getattr(record, which))


def main() -> int:
    global CACHE_ROOT

    if CACHE_ROOT is not None and CACHE_ROOT.exists():
        shutil.rmtree(CACHE_ROOT, ignore_errors=True)
    CACHE_ROOT = prepare_cache_root(MOTHER_DRIVE, f"{TEST_MARKER}Cache")
    if CACHE_ROOT is None:
        print(
            f"[跳过] 本机只有 {MOTHER_DRIVE}: 一个可用卷，无法做跨盘端到端测试"
            "（本工具的前提就是母盘与加速盘是两个盘）。"
        )
        return 0
    print(f"母盘 {MOTHER_DRIVE}:　加速盘 {drive_of(str(CACHE_ROOT))}:（缓存 {CACHE_ROOT}）")
    # 探测成功后立刻清掉：让测试从"缓存目录还不存在"的初始状态开始，
    # 顺便验证前置检查会提醒、加速时会自动创建。
    shutil.rmtree(CACHE_ROOT, ignore_errors=True)
    lib, game_dir = build_library()
    cfg = make_config(lib)
    store = StateStore(cfg=cfg)

    report = scan(cfg, state=store.to_dict(), steam_root=str(lib))
    game = report.find("777777")[0]
    check("扫描到游戏且状态为母盘中", game.status == ST_ON_HDD, game.status)
    check(
        "路径按 installdir 解析（显示名与目录名不同）",
        os.path.normcase(Path(game.game_path).name) == os.path.normcase("SampleGameDir"),
        game.game_path,
    )

    # ---------------- 0) 前置检查 ----------------
    pre = preflight_accelerate(game, cfg)
    check("加速前置检查通过", pre.ok, str(pre.blockers))
    check("前置检查包含缓存目录提醒", any("缓存" in w or "创建" in w for w in pre.warnings), str(pre.warnings))

    # ---------------- 1) 回滚路径：复制失败 + 用户拒绝删除半成品 ----------------
    refuse = ScriptedDeletionConfirmer(answers=[False])
    expect_raises(
        "复制中断时操作失败",
        OperationFailed,
        accelerate,
        game,
        cfg,
        store=store,
        engine=FailingEngine(),
        deletion_confirmer=refuse,
    )
    check("回滚后游戏目录回到原位（真实目录，非联接）", game_dir.is_dir() and not is_junction(game_dir))
    check("回滚时确实请求过删除半成品", len(refuse.requests) == 1, str(len(refuse.requests)))
    partial = Path(refuse.requests[0].target) if refuse.requests else None
    check("用户拒绝后半成品被保留", partial is not None and partial.exists(), str(partial))
    check("状态被标记为失败待修复", store.phase_of("777777") not in ("", PHASE_ACCELERATED), store.phase_of("777777"))
    if partial is not None and partial.exists():
        shutil.rmtree(partial, ignore_errors=True)  # 测试产物，清掉以便下一步重来
    store.remove("777777")

    # ---------------- 2) 回滚路径：复制失败 + 用户同意删除半成品 ----------------
    approve = ScriptedDeletionConfirmer(answers=[True])
    expect_raises(
        "复制中断时操作失败（第二次）",
        OperationFailed,
        accelerate,
        game,
        cfg,
        store=store,
        engine=FailingEngine(),
        deletion_confirmer=approve,
    )
    check("用户同意后半成品已清理", all(not Path(r.target).exists() for r in approve.requests), str(approve.requests))
    check("游戏目录仍在原位", game_dir.is_dir() and not is_junction(game_dir))
    check("状态记录已清除（回滚成功）", store.get("777777") is None, str(store.phase_of("777777")))

    # ---------------- 3) 正式加速 ----------------
    progress_events: list = []
    result = accelerate(game, cfg, store=store, on_progress=progress_events.append, deletion_confirmer=approve)
    check("加速成功", result.ok, result.detail)
    backup = path_of(result, "backup_path")
    cache_copy = path_of(result, "cache_copy")
    check("原位置变成目录联接", is_junction(game_dir))
    check("联接指向缓存副本", os.path.normcase(junction_target(game_dir)) == os.path.normcase(str(cache_copy)))
    check("母本备份存在", backup.is_dir() and backup.name == "SampleGameDir")
    check("母本备份位于 .hdd_cache 下", backup.parent.name == ".hdd_cache")
    check("缓存副本存在", cache_copy.is_dir())
    check("缓存副本与母本严格一致", verify_trees(backup, cache_copy, allow_extra=False).ok)
    check("状态记录为已加速", store.phase_of("777777") == PHASE_ACCELERATED, store.phase_of("777777"))
    check("加速过程有进度回调", len(progress_events) > 0, f"{len(progress_events)} 次")

    report2 = scan(cfg, state=store.to_dict(), steam_root=str(lib))
    check("扫描器识别为已加速", report2.find("777777")[0].status == ST_ACCELERATED)

    # ---------------- 4) 模拟 Steam 在 SSD 副本上更新 ----------------
    (cache_copy / "data" / "pack1.pak").write_bytes(b"Q" * 65536)          # 补丁变大
    (cache_copy / "dlc").mkdir()
    (cache_copy / "dlc" / "new.pak").write_bytes(b"N" * 8192)              # 新增 DLC
    (backup / "obsolete.dat").write_bytes(b"O" * 1024)                     # 母盘残留（Steam 已删）
    os.utime(backup / "obsolete.dat", (1700000000, 1700000000))

    # ---------------- 5) 回写：用户拒绝搬移 → 必须什么都不动 ----------------
    extras_before = sorted(p.relative_to(backup).as_posix() for p in backup.rglob("*"))
    refuse_extras = ScriptedExtrasConfirmer(answers=[False])
    expect_raises(
        "回写时用户拒绝搬移 → 中止",
        OperationBlocked,
        writeback,
        game,
        cfg,
        store=store,
        extras_confirmer=refuse_extras,
    )
    check("被拒绝后母盘目录内容未变", sorted(p.relative_to(backup).as_posix() for p in backup.rglob("*")) == extras_before)
    check("被拒绝后缓存副本未变", (cache_copy / "dlc" / "new.pak").exists())
    check("被拒绝后仍是加速状态", is_junction(game_dir))
    check("确认者确实看到了清单", len(refuse_extras.requests) == 1 and "obsolete.dat" in refuse_extras.requests[0][1])

    # ---------------- 6) 回写：确认 → 搬入隔离区 + 镜像 ----------------
    approve_extras = ScriptedExtrasConfirmer(answers=[True])
    wb = writeback(game, cfg, store=store, extras_confirmer=approve_extras)
    check("回写成功", wb.ok, wb.detail)
    check("多余文件已移入隔离区", bool(wb.quarantine_item) and Path(wb.quarantine_item).is_dir(), wb.quarantine_item)
    check("母盘上多余文件已消失", not (backup / "obsolete.dat").exists())
    check("新 DLC 已回写母盘", (backup / "dlc" / "new.pak").exists())
    check("补丁已更新", (backup / "data" / "pack1.pak").stat().st_size == 65536)
    check("回写后两侧严格一致", verify_trees(cache_copy, backup, allow_extra=False).ok)
    check("回写后仍是加速状态（联接还在）", is_junction(game_dir))
    check("状态记录了最近回写时间", (store.get("777777").last_writeback or 0) > 0)
    check("状态记录了隔离项", store.get("777777").quarantine_item == wb.quarantine_item)

    # ---------------- 7) 释放：拒绝删除缓存副本 → 必须什么都不动 ----------------
    refuse_del = ScriptedDeletionConfirmer(answers=[False])
    expect_raises(
        "释放时用户拒绝删除缓存副本 → 中止",
        DeletionNotConfirmed,
        release,
        game,
        cfg,
        store=store,
        deletion_confirmer=refuse_del,
        extras_confirmer=approve_extras,
    )
    check("被拒绝后联接仍在", is_junction(game_dir))
    check("被拒绝后缓存副本仍在", cache_copy.is_dir())
    check("被拒绝后母本仍备份在 .hdd_cache", backup.is_dir())

    # ---------------- 8) 释放：确认 → 撤掉加速 ----------------
    approve_del = ScriptedDeletionConfirmer(answers=[True])
    rel = release(
        game,
        cfg,
        store=store,
        deletion_confirmer=approve_del,
        extras_confirmer=approve_extras,
    )
    check("释放成功", rel.ok, rel.detail)
    check("不再是目录联接（母本已回到该路径成为真实目录）", not is_junction(game_dir))
    check("游戏目录回到 common 下（真实目录）", game_dir.is_dir() and not is_junction(game_dir))
    check("缓存副本已删除", not cache_copy.exists())
    check("母本备份位置已不存在", not backup.exists())
    check("状态记录已清除", store.get("777777") is None)
    check("游戏内容完整（含更新后的补丁与 DLC）", (game_dir / "data" / "pack1.pak").stat().st_size == 65536 and (game_dir / "dlc" / "new.pak").exists())
    check("释放时确实请求过删除缓存副本", len(approve_del.requests) == 1, str(len(approve_del.requests)))

    report3 = scan(cfg, state=store.to_dict(), steam_root=str(lib))
    check("扫描器识别为回到母盘", report3.find("777777")[0].status == ST_ON_HDD)

    # ---------------- 9) 审计日志（写在测试沙箱的日志目录里） ----------------
    from logger import current_log_dir

    log_file = current_log_dir() / "operations.log"
    content = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
    for action in ("accelerate_start", "accelerate_done", "writeback_start", "writeback_done", "release_done"):
        check(f"审计日志含 {action}", f"action={action}" in content)

    # ---------------- 收尾 ----------------
    # 顺序很重要：先做还会写日志的动作（清隔离项），再关日志句柄，最后删沙箱——
    # 否则日志文件被打开着，Windows 删不掉，或者被后续日志重新创建出来。
    drive = MOTHER_DRIVE
    trash = trash_root(drive)
    if trash.is_dir():
        for entry in list_items(drive):
            if TEST_MARKER in entry.name:
                purge_item(entry.item_dir, drive, ScriptedDeletionConfirmer(answers=[True]))
        try:
            if not any(trash.iterdir()):
                trash.rmdir()
        except OSError:
            pass
    from logger import close_loggers

    close_loggers()
    shutil.rmtree(SANDBOX, ignore_errors=True)
    shutil.rmtree(CACHE_ROOT, ignore_errors=True)
    check("测试沙箱已清理", not SANDBOX.exists())
    check("测试缓存目录已清理", not CACHE_ROOT.exists())
    check("隔离区无本测试残留", not any(TEST_MARKER in e.name for e in list_items(drive)))

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
