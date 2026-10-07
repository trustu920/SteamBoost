"""阶段 2 地基验证：Junction 原语与进程安全闸。

这个测试的重点不是"功能能跑"，而是**危险操作会不会被拒绝**：

  1. 创建联接后能回读验证目标；
  2. 删除联接只删联接，目标内容必须完好；
  3. 对**真实目录**调用 remove_junction 必须被拒绝（这是防误删的最后一道闸）；
  4. 目标已存在时创建联接必须失败，不能覆盖已有目录；
  5. 路径白名单 assert_within 在不该放行时必须抛异常；
  6. 进程检查能发现"映像路径位于某目录之内"的进程（用放在该目录里的 ping.exe 模拟游戏进程）。

运行： python tests/test_safety_primitives.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 日志重定向到测试沙箱（必须在导入应用模块之前设置，避免污染真实审计日志）
os.environ["STEAMBOOST_LOG_DIR"] = str(Path(__file__).resolve().parent / "_sandbox" / "logs")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from junction_utils import (  # noqa: E402
    JunctionError,
    PathSafetyError,
    assert_within,
    create_junction,
    is_junction,
    is_within,
    junction_target,
    remove_junction,
)
from process_guard import game_processes, is_steam_running, preflight  # noqa: E402

SANDBOX = Path(__file__).resolve().parent / "_sandbox"

failures: list[str] = []


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


def main() -> int:
    if SANDBOX.exists():
        # 先清掉可能存在的联接，避免 rmtree 顺着联接删进目标
        for path in SANDBOX.rglob("*"):
            if path.is_dir() and is_junction(path):
                remove_junction(path)
        shutil.rmtree(SANDBOX, ignore_errors=True)

    target = SANDBOX / "cache" / "100000_SampleGame"
    (target / "data").mkdir(parents=True)
    (target / "data" / "big.bin").write_bytes(b"a" * 4096)
    link = SANDBOX / "lib" / "common" / "SampleGame"
    link.parent.mkdir(parents=True, exist_ok=True)

    # 1) 创建 + 回读验证
    created = create_junction(link, target)
    check("创建联接成功", is_junction(link), str(link))
    check(
        "联接目标与预期一致",
        os.path.normcase(created) == os.path.normcase(str(target)),
        f"{created} != {target}",
    )
    check("通过联接能读到目标内容", (link / "data" / "big.bin").exists())

    # 2) 目标已存在时创建联接必须失败
    expect_raises("目标位置被占用时拒绝创建", JunctionError, create_junction, link, target)

    # 3) 真实目录不允许被 remove_junction 删除
    real_dir = SANDBOX / "lib" / "common" / "RealGame"
    real_dir.mkdir(parents=True, exist_ok=True)
    (real_dir / "keep.bin").write_bytes(b"k" * 16)
    expect_raises("对真实目录删除被拒绝", PathSafetyError, remove_junction, real_dir)
    check("真实目录内容未被删除", (real_dir / "keep.bin").exists())

    # 4) 白名单校验
    check("is_within 正常放行", is_within(target / "data" / "big.bin", target))
    check("is_within 拒绝越界路径", not is_within(SANDBOX / "other", target))
    expect_raises("assert_within 越界时抛异常", PathSafetyError, assert_within, SANDBOX / "other", target)

    # 5) 删除联接：目标必须完好
    remove_junction(link)
    check("联接已消失", not os.path.lexists(str(link)))
    check("目标目录仍然存在", target.is_dir())
    check("目标内容完好", (target / "data" / "big.bin").read_bytes() == b"a" * 4096)
    check("原位置不再存在", not link.exists())

    # 6) 再建一次，验证可重复使用
    create_junction(link, target)
    check("二次创建联接成功", is_junction(link))
    check("二次创建后目标解析正确", os.path.normcase(junction_target(link)) == os.path.normcase(str(target)))

    # 7) 进程检查：把一个可执行文件放进"游戏目录"再运行它
    fake_game = SANDBOX / "lib" / "common" / "RunningGame"
    fake_game.mkdir(parents=True, exist_ok=True)
    ping_src = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "ping.exe"
    ping_dst = fake_game / "ping.exe"
    proc = None
    if ping_src.is_file():
        shutil.copy2(ping_src, ping_dst)
        proc = subprocess.Popen(
            [str(ping_dst), "-n", "20", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        time.sleep(1.5)
        found = game_processes(fake_game)
        check("能识别游戏目录内运行的进程", any(pid == proc.pid for pid, _, _ in found), str(found))
        result = preflight(fake_game, require_steam_closed=False)
        check("前置检查因游戏进程阻塞", not result.ok, str(result.blockers))
    else:
        check("找到 ping.exe 用作模拟进程", False, str(ping_src))

    # 8) Steam 客户端状态只做观察（本机当前未运行 Steam）
    running, pids = is_steam_running()
    print(f"[INFO] Steam 客户端运行中：{running} {pids}")

    # 清理：先杀模拟进程，再删联接，最后删沙箱
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    remove_junction(link)
    from logger import close_loggers

    close_loggers()
    shutil.rmtree(SANDBOX, ignore_errors=True)
    check("清理后沙箱已移除", not SANDBOX.exists())

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
