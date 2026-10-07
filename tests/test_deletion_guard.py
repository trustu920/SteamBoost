"""删除闸门验证：**没有人类确认，任何删除都不可能发生**。

安全原则（用户明确要求，务必遵守）
----------------------------------
**绝不针对真实路径发出删除请求，哪怕是期望被拒绝的负向用例。**
理由：一旦拦截逻辑有 bug，被拒的用例就变成真实的数据毁灭。

因此本测试的所有"危险靶子"都是**安全的替代物**：

* "盘根"用**不存在的盘符**（先断言该盘不存在）验证；
* "Steam 库路径"用**沙箱内伪造的** ``…\\steamapps\\common\\SomeGame``；
* "越界路径"用沙箱内的普通目录；
* "系统目录保护"通过**临时替换受保护根列表**来验证，不碰任何真实系统路径；
* 另有一道 :func:`assert_in_sandbox` 铁律：任何要交给删除 API 的目标必须位于沙箱内，
  否则测试直接中止（而不是去删）。

覆盖：
  1. 没有确认者 → 拒绝（默认拒绝，fail-closed）；
  2. 确认者拒绝 → 拒绝，文件原封不动；
  3. 确认者同意 → 才真的删除；
  4. 路径校验不过 → 连问都不问就拒绝；
  5. 确认前能看到确切目录、文件数、字节数与样例；
  6. 每次请求与决定都进审计日志。

运行： python tests/test_deletion_guard.py
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 日志重定向到测试沙箱（必须在导入应用模块之前设置，避免污染真实审计日志）
os.environ["STEAMBOOST_LOG_DIR"] = str(Path(__file__).resolve().parent / "_guard_sandbox" / "logs")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deletion_guard  # noqa: E402
from config import HDD_CACHE_DIR_NAME, app_data_dir  # noqa: E402
from deletion_guard import (  # noqa: E402
    KIND_CACHE_COPY,
    DeletionNotConfirmed,
    DeletionRefused,
    DeletionRequest,
    build_request,
    delete,
    safe_remove_tree,
    validate_target,
)
from junction_utils import create_junction, is_junction, is_within, remove_junction  # noqa: E402
from quarantine import MANIFEST_NAME, list_items, purge_item, quarantine_extras  # noqa: E402

SANDBOX = Path(__file__).resolve().parent / "_guard_sandbox"
failures: list[str] = []

#: 测试创建的隔离项都带这个标记，收尾时**只清理带标记的项**
TEST_MARKER = "SteamBoostTest"


@dataclass
class ScriptedConfirmer:
    """测试专用确认者：按脚本回答，并记录收到的每一个请求。"""

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


def assert_in_sandbox(path: str | os.PathLike[str]) -> None:
    """测试铁律：交给删除 API 的目标必须位于一次性沙箱内。

    这是为了防止测试代码将来被改动时，不小心把真实目录当成靶子。
    """
    if not is_within(path, SANDBOX):
        raise AssertionError(f"测试用例试图操作沙箱之外的路径，已中止：{path}")


def make_copy_in_sandbox(name: str, files: int = 3, size: int = 1024) -> Path:
    """在沙箱里造一个"缓存副本"目录（只有测试数据）。"""
    target = SANDBOX / "cache" / name
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    for index in range(files):
        (target / f"file{index}.bin").write_bytes(b"x" * size)
    return target


def safe_delete(kind: str, target: Path, allowed_root: Path, **kwargs) -> int:
    """包装 delete()，强制目标在沙箱内。"""
    assert_in_sandbox(target)
    return delete(kind, target, allowed_root, **kwargs)


def main() -> int:
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    SANDBOX.mkdir(parents=True)
    cache_root = SANDBOX / "cache"
    cache_root.mkdir(parents=True, exist_ok=True)

    # ---------------- 1) 没有确认者 → 默认拒绝 ----------------
    target = make_copy_in_sandbox("100000_NoConfirmer", files=2, size=512)
    expect_raises(
        "无确认者时拒绝删除",
        DeletionNotConfirmed,
        safe_delete,
        KIND_CACHE_COPY,
        target,
        cache_root,
        reason="测试：无确认者",
    )
    check("无确认者时目标完好", target.is_dir() and len(list(target.iterdir())) == 2)

    # ---------------- 2) 确认者拒绝 → 拒绝且不动文件 ----------------
    deny = ScriptedConfirmer(answers=[False])
    expect_raises(
        "确认者拒绝时抛出 DeletionNotConfirmed",
        DeletionNotConfirmed,
        safe_delete,
        KIND_CACHE_COPY,
        target,
        cache_root,
        confirmer=deny,
        reason="测试：用户取消",
    )
    check("用户取消后目标完好", target.is_dir())
    check("确认者确实收到了请求", len(deny.requests) == 1)

    # ---------------- 3) 确认前能看到确切信息 ----------------
    request = build_request(KIND_CACHE_COPY, target, cache_root, reason="测试：预览")
    check("预览包含文件数", request.files == 2, str(request.files))
    check("预览包含字节数", request.bytes == 1024, str(request.bytes))
    check("预览包含样例文件名", any(name.startswith("file") for name in request.sample), str(request.sample))
    summary = request.summary()
    check("摘要里出现完整路径", str(target) in summary)
    check("摘要里出现不可撤销提示", "不可撤销" in summary, summary)

    # ---------------- 4) 确认者同意 → 才真的删除 ----------------
    approve = ScriptedConfirmer(answers=[True])
    freed = safe_delete(
        KIND_CACHE_COPY,
        target,
        cache_root,
        confirmer=approve,
        reason="测试：用户确认",
    )
    check("确认后删除成功", not target.exists())
    check("返回释放字节数正确", freed == 1024, str(freed))
    check("确认者只被询问一次", len(approve.requests) == 1)

    # ---------------- 5) 路径校验：连问都不问就拒绝（全部使用安全靶子） ----------------
    outside = SANDBOX / "outside" / "victim"
    outside.mkdir(parents=True)
    (outside / "keep.bin").write_bytes(b"k" * 64)
    asker = ScriptedConfirmer(answers=[True, True, True, True])

    expect_raises(
        "允许根之外的路径被拒绝",
        DeletionRefused,
        safe_delete,
        KIND_CACHE_COPY,
        outside,
        cache_root,
        confirmer=asker,
        reason="测试：越界",
    )
    check("越界目标完好", outside.is_dir() and (outside / "keep.bin").exists())

    # 盘根：用**不存在的盘符 + 纯校验函数**，绝不指向真实盘根。
    # validate_target 是纯函数（只做路径判断与属性读取），不会删除任何东西。
    absent_drive = next(
        (letter for letter in "QRSTUVWXY" if not os.path.exists(f"{letter}:\\")),
        "",
    )
    if absent_drive:
        expect_raises(
            "盘根被拒绝（使用不存在的盘符验证）",
            DeletionRefused,
            validate_target,
            KIND_CACHE_COPY,
            f"{absent_drive}:\\",
            f"{absent_drive}:\\",
        )
    else:
        check("找到可用的不存在盘符", False, "Q-X 都被占用了")

    # Steam 库路径：用沙箱内伪造的 …\steamapps\common\ 路径。
    # 即使拦截完全失效，最坏结果也只是删掉这个假游戏目录。
    fake_lib = SANDBOX / "lib"
    steam_like = fake_lib / "steamapps" / "common" / "SomeGame"
    steam_like.mkdir(parents=True)
    (steam_like / "game.exe").write_bytes(b"g" * 32)
    expect_raises(
        "Steam 库路径下的目录被拒绝",
        DeletionRefused,
        safe_delete,
        KIND_CACHE_COPY,
        steam_like,
        fake_lib,
        confirmer=asker,
        reason="测试：库内",
    )
    check("模拟游戏目录完好", (steam_like / "game.exe").exists())
    check("被拒的请求没有进入确认环节", len(asker.requests) == 0, str(len(asker.requests)))

    # 系统/用户目录保护：临时把沙箱本身标记为受保护根来验证这条逻辑，
    # 这样既能覆盖代码路径，又完全不接触真实系统目录。
    original_roots = deletion_guard.protected_roots
    try:
        deletion_guard.protected_roots = lambda: [str(SANDBOX)]  # type: ignore[assignment]
        protected_target = make_copy_in_sandbox("100002_Protected", files=1, size=64)
        expect_raises(
            "受保护根之内的目标被拒绝（模拟系统目录保护）",
            DeletionRefused,
            safe_delete,
            KIND_CACHE_COPY,
            protected_target,
            cache_root,
            confirmer=asker,
            reason="测试：受保护根",
        )
        check("受保护靶子完好", protected_target.is_dir())
    finally:
        deletion_guard.protected_roots = original_roots  # type: ignore[assignment]

    # 目录联接必须走 remove_junction，不能按目录删除
    link_target = make_copy_in_sandbox("100001_LinkedTarget", files=1, size=128)
    link = SANDBOX / "link"
    create_junction(link, link_target)
    expect_raises(
        "目录联接不能按目录删除",
        DeletionRefused,
        safe_delete,
        KIND_CACHE_COPY,
        link,
        SANDBOX,
        confirmer=asker,
        reason="测试：联接",
    )
    check("联接仍然存在", is_junction(link))
    remove_junction(link)
    check("用 remove_junction 可以正常摘除联接", not os.path.lexists(str(link)))
    check("联接目标未被删除", link_target.is_dir())

    # ---------------- 6) 凭据不可绕过 ----------------
    try:
        safe_remove_tree(build_request(KIND_CACHE_COPY, link_target, cache_root))  # type: ignore[arg-type]
        check("未确认的请求不能直接执行删除", False, "竟然执行成功了")
    except (AttributeError, TypeError, DeletionRefused):
        check("未确认的请求不能直接执行删除", True)
    check("直接传请求对象时目标未被删除", link_target.is_dir())

    # ---------------- 7) 隔离区删除同样受闸门约束 ----------------
    mother = SANDBOX / "lib2" / "steamapps" / "common" / HDD_CACHE_DIR_NAME / "GameY"
    mother.mkdir(parents=True)
    (mother / "new.dat").write_bytes(b"n" * 256)
    (mother / "old_only.dat").write_bytes(b"o" * 512)
    ssd = SANDBOX / "cache2" / "200000_GameY"
    ssd.mkdir(parents=True)
    (ssd / "new.dat").write_bytes(b"n" * 256)
    drive = os.path.splitdrive(str(SANDBOX))[0].rstrip(":\\").upper()
    item = quarantine_extras(
        ["old_only.dat"], mother, appid="200000", game_name=f"Game Y {TEST_MARKER}", mother_drive=drive
    )
    check("隔离项已建立", (Path(item.item_dir) / MANIFEST_NAME).is_file())

    expect_raises(
        "隔离项在无确认者时无法删除",
        DeletionNotConfirmed,
        purge_item,
        item.item_dir,
        drive,
    )
    check("未确认时隔离项仍在", Path(item.item_dir).is_dir())

    expect_raises(
        "隔离项在用户取消时无法删除",
        DeletionNotConfirmed,
        purge_item,
        item.item_dir,
        drive,
        ScriptedConfirmer(answers=[False]),
    )
    check("取消后隔离项仍在", Path(item.item_dir).is_dir())

    approve2 = ScriptedConfirmer(answers=[True])
    freed2 = purge_item(item.item_dir, drive, approve2)
    check("用户确认后隔离项被删除", not Path(item.item_dir).exists())
    check("隔离项释放字节数 >= 数据字节", freed2 >= 512, str(freed2))

    # ---------------- 8) 审计日志可追溯（写在测试沙箱的日志目录里） ----------------
    from logger import current_log_dir

    log_file = current_log_dir() / "operations.log"
    content = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
    check("审计日志记录了被拒的删除请求", "result=refused" in content)
    check("审计日志记录了确认后的删除执行", "action=delete_executed" in content)
    check("审计日志包含删除目标路径", str(cache_root) in content)

    # 收尾：先清理本测试产生的隔离项（还会写日志），再关日志句柄，最后删沙箱
    trash = Path(f"{drive}:\\") / "SteamBoostTrash"
    if trash.is_dir():
        for entry in list_items(drive):
            if TEST_MARKER in entry.name:
                purge_item(entry.item_dir, drive, ScriptedConfirmer(answers=[True]))
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
