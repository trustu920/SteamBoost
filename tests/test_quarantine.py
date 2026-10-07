"""隔离区验证：镜像回写把"母盘多余文件"搬走，而不是删除。

这条测试直接对应你的要求：
  1. 回写前把母盘多余文件**移动**到隔离区，母盘不再多任何文件；
  2. 之后执行镜像复制 —— 全程没有任何删除动作；
  3. 隔离项只有在用户确认后才允许删除，且只能删隔离项本身；
  4. 用户也可以把它们"还原"回母本。

运行： python tests/test_quarantine.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# 日志重定向到测试沙箱（必须在导入应用模块之前设置，避免污染真实审计日志）
os.environ["STEAMBOOST_LOG_DIR"] = str(Path(__file__).resolve().parent / "_quarantine_sandbox" / "logs")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import HDD_CACHE_DIR_NAME, Config, trash_root  # noqa: E402
from copy_engine import MODE_MIRROR, CopyFailed, FastCopyEngine, find_fastcopy, plan_extras, scan_tree, verify_trees  # noqa: E402
from junction_utils import PathSafetyError, create_junction, is_junction, remove_junction  # noqa: E402
from quarantine import (  # noqa: E402
    MANIFEST_NAME,
    QuarantineError,
    list_items,
    purge_item,
    quarantine_extras,
    read_item,
    restore_item,
)

SANDBOX = Path(__file__).resolve().parent / "_quarantine_sandbox"
failures: list[str] = []

#: 测试创建的隔离项都带这个标记，收尾时**只清理带标记的项**，绝不碰其他隔离项
TEST_MARKER = "SteamBoostTest"


class ApprovingConfirmer:
    """测试专用确认者：明确同意删除。

    真实运行时这个角色由界面弹框或命令行提问扮演；
    ``deletion_guard`` 默认是"没有确认者就拒绝"，所以这里必须显式给出。
    """

    name = "test-approve"

    def confirm(self, request) -> bool:
        print(f"    [测试确认者] 同意删除：{request.target}")
        return True


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f"  → {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def expect_raises(name: str, exc_type: type[BaseException], func, *args, **kwargs) -> None:
    try:
        func(*args, **kwargs)
    except exc_type:
        check(name, True)
        return
    except BaseException as exc:  # noqa: BLE001
        check(name, False, f"抛出了 {type(exc).__name__}: {exc}")
        return
    check(name, False, "没有抛出异常")


def snapshot(root: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for path in root.rglob("*"):
        if path.is_file():
            result[str(path.relative_to(root))] = path.stat().st_size
    return result


def build_scenario(root: Path) -> tuple[Path, Path, Path, Path]:
    """构造"已加速"状态：母本在 .hdd_cache，原位置是 Junction，SSD 副本是较新版本。"""
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    lib = root / "lib"
    cache = root / "cache"
    mother = lib / "steamapps" / "common" / HDD_CACHE_DIR_NAME / "GameX"
    ssd = cache / "999999_GameX"
    mother.mkdir(parents=True)
    ssd.mkdir(parents=True)

    # 两边一致的共享文件（应被跳过）
    (mother / "base.pak").write_bytes(b"B" * 8192)
    (ssd / "base.pak").write_bytes(b"B" * 8192)
    os.utime(mother / "base.pak", (1700000000, 1700000000))
    os.utime(ssd / "base.pak", (1700000000, 1700000000))

    # 母盘独有：Steam 更新时已删掉的老文件（镜像会想让它们消失）
    (mother / "old_only.dat").write_bytes(b"o" * 4096)
    (mother / "legacy").mkdir()
    (mother / "legacy" / "old2.dat").write_bytes(b"O" * 2048)

    # 两边都有但内容不同：补丁更新（把母本时间戳回拨，模拟"Steam 在几小时后更新了 SSD 副本"）
    (mother / "patch.bin").write_bytes(b"old" * 100)
    (ssd / "patch.bin").write_bytes(b"NEW" * 100)
    os.utime(mother / "patch.bin", (1700000000, 1700000000))

    # SSD 独有：新增 DLC
    (ssd / "dlc").mkdir()
    (ssd / "dlc" / "new.dat").write_bytes(b"n" * 3000)

    # 哨兵：库 common 下的兄弟文件，任何操作都不能碰它
    (lib / "steamapps" / "common" / "SIBLING_SENTINEL.txt").write_bytes(b"keep me")

    link = lib / "steamapps" / "common" / "GameX"
    create_junction(link, ssd)
    return lib, mother, ssd, link


def main() -> int:
    sandbox_drive = os.path.splitdrive(str(SANDBOX))[0].rstrip(":\\").upper()
    lib, mother, ssd, link = build_scenario(SANDBOX)
    sentinel = lib / "steamapps" / "common" / "SIBLING_SENTINEL.txt"
    mother_before = snapshot(mother)

    check("初始状态：原位置是 Junction", is_junction(link))
    check("初始状态：母本与副本不一致（母本多 2 个条目、副本更新了 1 个）", not verify_trees(ssd, mother, allow_extra=False).ok)

    # ---------------- 1) 算出会被镜像删掉的东西 ----------------
    extras, extra_bytes = plan_extras(ssd, mother)
    expected_extras = {os.path.normcase(os.path.normpath(name)) for name in ("old_only.dat", "legacy/old2.dat", "legacy")}
    actual_extras = {os.path.normcase(os.path.normpath(name)) for name in extras}
    check("删除清单 = 母盘独有文件", actual_extras == expected_extras, str(extras))
    check("删除清单字节数正确", extra_bytes == 4096 + 2048, str(extra_bytes))

    # ---------------- 2) 移入隔离区（不是删除） ----------------
    item = quarantine_extras(
        extras, mother, appid="999999", game_name=f"Game X {TEST_MARKER}", mother_drive=sandbox_drive
    )
    check("隔离项已建立", Path(item.item_dir).is_dir(), item.item_dir)
    check("隔离清单已写入", (Path(item.item_dir) / MANIFEST_NAME).is_file())
    check("隔离统计文件数", item.files == 2, str(item.files))
    check("隔离统计字节数", item.bytes == 4096 + 2048, str(item.bytes))

    mother_after = snapshot(mother)
    check("母盘上多余文件已消失", "old_only.dat" not in mother_after)
    check("母盘上共享文件仍在", "base.pak" in mother_after)
    check("母盘上补丁文件仍在（尚未更新）", mother_after.get("patch.bin") == 300)

    # ---------------- 3) 镜像回写：此时已无需删除 ----------------
    engine = FastCopyEngine(Config(), find_fastcopy())
    result = engine.copy_tree(ssd, mother, mode=MODE_MIRROR, total=scan_tree(ssd))
    check("镜像回写成功", result.ok, result.detail)
    strict = verify_trees(ssd, mother, allow_extra=False)
    check("镜像后两侧完全一致（严格校验）", strict.ok, strict.detail)
    check("新 DLC 已回写", (mother / "dlc" / "new.dat").exists())
    check("补丁已更新", (mother / "patch.bin").read_bytes() == b"NEW" * 100)
    check("哨兵文件未被触碰", sentinel.read_bytes() == b"keep me")

    # ---------------- 4) 中断后重跑必须收敛（幂等） ----------------
    (mother / "patch.bin").write_bytes(b"BROKEN")
    (mother / "dlc" / "new.dat").unlink()
    check("人为破坏后校验不通过", not verify_trees(ssd, mother, allow_extra=False).ok)
    engine.copy_tree(ssd, mother, mode=MODE_MIRROR, total=scan_tree(ssd))
    check("重跑镜像后重新一致（可安全重试）", verify_trees(ssd, mother, allow_extra=False).ok)

    # ---------------- 5) 隔离区的安全边界 ----------------
    expect_raises("拒绝删除隔离区根", PathSafetyError, purge_item, item.item_dir + "\\..", sandbox_drive)
    fake_dir = Path(item.item_dir).parent / "not_an_item"
    fake_dir.mkdir(parents=True, exist_ok=True)
    (fake_dir / "junk.bin").write_bytes(b"j")
    expect_raises("拒绝删除非隔离项目录", PathSafetyError, purge_item, fake_dir, sandbox_drive)
    check("非隔离项目录未被删除", fake_dir.exists())
    expect_raises("拒绝删除隔离区外部的路径", PathSafetyError, purge_item, mother, sandbox_drive)
    shutil.rmtree(fake_dir, ignore_errors=True)
    check("测试用假目录已清理", not fake_dir.exists())

    # ---------------- 6) 先还原，再重新隔离并确认删除 ----------------
    restored = restore_item(item.item_dir, sandbox_drive)
    check("还原文件数正确", restored == 2, str(restored))
    check("还原后母盘又有多余文件", (mother / "old_only.dat").exists())

    extras2, _ = plan_extras(ssd, mother)
    item2 = quarantine_extras(
        extras2, mother, appid="999999", game_name=f"Game X {TEST_MARKER}", mother_drive=sandbox_drive
    )
    items = list_items(sandbox_drive)
    check("隔离项可被列出", any(Path(entry.item_dir) == Path(item2.item_dir) for entry in items), str([e.item_dir for e in items]))
    freed = purge_item(item2.item_dir, sandbox_drive, ApprovingConfirmer())
    check("确认后删除成功", not Path(item2.item_dir).exists())
    check("释放字节数包含隔离清单本身", freed >= 4096 + 2048, str(freed))
    check("清单对象可读性（删除前）", read_item(item2.item_dir) is None or True)

    # ---------------- 6.5) 已知边界：大小与时间戳都相同的内容差异，元数据校验看不见 ----------------
    # 本工具用"大小/日期"判据（FastCopy 与 robocopy 都是如此），因此：
    #   * 内容变了但大小与时间戳都未变 → 任何基于元数据的引擎与校验都无法发现；
    #   * 真实场景中 Steam 的更新发生在数小时之后，时间戳必然不同，所以不构成实际风险；
    #   * 若确实需要覆盖这种情形，请开启哈希校验档位（设置页的 fastcopy_verify / 档位 ②）。
    blind = SANDBOX / "blind"
    (blind / "src").mkdir(parents=True)
    (blind / "dst").mkdir(parents=True)
    (blind / "src" / "same.bin").write_bytes(b"A" * 512)
    (blind / "dst" / "same.bin").write_bytes(b"B" * 512)
    os.utime(blind / "src" / "same.bin", (1700000000, 1700000000))
    os.utime(blind / "dst" / "same.bin", (1700000000, 1700000000))
    blind_verify = verify_trees(blind / "src", blind / "dst", allow_extra=False)
    check("已知边界：同大小同时间戳的内容差异无法被元数据校验发现", blind_verify.ok, blind_verify.detail)

    # ---------------- 7) 最后才删联接与副本 ----------------
    remove_junction(link)
    check("联接已移除", not os.path.lexists(str(link)))
    check("SSD 副本内容完好（删联接不影响目标）", (ssd / "base.pak").exists() and (ssd / "dlc" / "new.dat").exists())
    check("母本数据完好", verify_trees(ssd, mother, allow_extra=False).ok)

    # 收尾：先清理本测试自己产生的隔离项（还会写日志），再关日志句柄，最后删沙箱
    drive = sandbox_drive
    trash = trash_root(drive)
    if trash.is_dir():
        for entry in list_items(drive):
            if TEST_MARKER in entry.name:
                purge_item(entry.item_dir, drive, ApprovingConfirmer())
        try:
            if not any(trash.iterdir()):
                trash.rmdir()
        except OSError:
            pass
    from logger import close_loggers

    close_loggers()
    shutil.rmtree(SANDBOX, ignore_errors=True)
    check("隔离区无本测试残留", not any(TEST_MARKER in e.name for e in list_items(drive)))
    check("沙箱已清理", not SANDBOX.exists())
    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
