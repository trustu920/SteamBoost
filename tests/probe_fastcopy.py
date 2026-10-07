"""FastCopy CLI 行为实测（阶段 2 证据采集，不是单元测试）。

为什么要实测：文档只写了"应该怎样"，而真实行为（参数顺序、退出码、
删除语义、日志格式）必须在本机确认，否则引擎写完就是猜的。

关键约束（实测确认）：
  * ``/to=`` 必须是最后一个参数，否则 fcp.exe 报 "Too few/many argument"
    并弹出错误对话框（用隐藏窗口运行时会表现为"卡死"）。
  * ``/cmd=diff``  = 差异复制（大小/日期不同或不存在才复制），不删除目标多余文件。
  * ``/cmd=sync``  = 差异复制 + 删除目标中源没有的文件（镜像语义）。
  * ``/cmd=force_copy`` = 无脑全覆盖。

运行： python tests/probe_fastcopy.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copy_engine import find_fastcopy  # noqa: E402

SANDBOX = Path(__file__).resolve().parent / "_fcprobe"
#: FastCopy 的日志文件就在它自己的安装目录里（路径由检测结果推导，不写死）
FC_LOG: Path | None = None


def find_fcp() -> Path | None:
    """复用程序里的检测逻辑：注册表 → 环境变量 → 自带目录 → PATH。"""
    return find_fastcopy()


def build_fixture() -> tuple[Path, Path]:
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    src, dst = SANDBOX / "src", SANDBOX / "dst"
    (src / "sub").mkdir(parents=True)
    dst.mkdir(parents=True)
    (src / "a.txt").write_bytes(b"a" * 100)
    (src / "b.txt").write_bytes(b"b" * 200)
    (src / "sub" / "c.txt").write_bytes(b"c" * 300)
    # 目标里：a.txt 与源一致（应被跳过）、b.txt 大小不同（应被覆盖）、extra.txt 源里没有（sync 应删除）
    shutil.copy2(src / "a.txt", dst / "a.txt")
    (dst / "b.txt").write_bytes(b"x" * 999)
    (dst / "extra.txt").write_bytes(b"e" * 50)
    return src, dst


def snapshot(root: Path) -> list[str]:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rows.append(f"{path.relative_to(root)}  {path.stat().st_size}B")
    return rows


def run_fcp(fcp: Path, args: list[str], timeout: int = 120) -> tuple[int, str, str, float]:
    """运行 fcp.exe；/to= 必须放在最后（调用方负责）。"""
    started = time.time()
    try:
        result = subprocess.run(
            [str(fcp), *args],
            capture_output=True,
            timeout=timeout,
        )
        elapsed = time.time() - started
        out = result.stdout.decode("mbcs", errors="replace")
        err = result.stderr.decode("mbcs", errors="replace")
        return result.returncode, out, err, elapsed
    except subprocess.TimeoutExpired:
        return -999, "", f"超时 {timeout}s，已强制结束", time.time() - started


def show(title: str, rows: list[str]) -> None:
    print(f"--- {title} ---")
    for row in rows:
        print("   ", row)


def main() -> int:
    global FC_LOG

    fcp = find_fcp()
    if fcp is None:
        print("未找到 fcp.exe，跳过实测")
        return 2
    print(f"引擎：{fcp}")
    # FastCopy 的自身日志就在安装目录里，路径由检测结果推导
    FC_LOG = fcp.parent / "FastCopy.log"

    src, dst = build_fixture()
    show("运行前 dst", snapshot(dst))

    log1 = SANDBOX / "run1.log"
    code, out, err, secs = run_fcp(
        fcp,
        ["/cmd=diff", "/no_ui", "/log", f"/logfile={log1}", str(src), f"/to={dst}"],
    )
    print(f"\n[run1 /cmd=diff] exit={code} 用时={secs:.2f}s")
    print(f"  stdout={out.strip()[:300]!r}")
    print(f"  stderr={err.strip()[:300]!r}")
    show("运行后 dst", snapshot(dst))

    log2 = SANDBOX / "run2.log"
    code2, out2, err2, secs2 = run_fcp(
        fcp,
        ["/cmd=sync", "/no_ui", "/log", f"/logfile={log2}", str(src), f"/to={dst}"],
    )
    print(f"\n[run2 /cmd=sync] exit={code2} 用时={secs2:.2f}s")
    print(f"  stdout={out2.strip()[:300]!r}")
    print(f"  stderr={err2.strip()[:300]!r}")
    show("运行后 dst（extra.txt 应已消失）", snapshot(dst))

    log3 = SANDBOX / "run3.log"
    code3, _, _, secs3 = run_fcp(
        fcp,
        ["/cmd=sync", "/verify=TRUE", "/no_ui", "/log", f"/logfile={log3}", str(src), f"/to={dst}"],
    )
    print(f"\n[run3 /cmd=sync /verify=TRUE] exit={code3} 用时={secs3:.2f}s")

    print("\n--- 我们指定的日志文件（run1）---")
    if log1.is_file():
        print(log1.read_text(encoding="utf-8", errors="replace")[:1500])

    print("\n--- FastCopy 自身日志尾部 ---")
    if FC_LOG is not None and FC_LOG.is_file():
        lines = FC_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-24:]:
            print("   ", line)

    shutil.rmtree(SANDBOX, ignore_errors=True)
    print("\n探测目录已清理")
    return 0


if __name__ == "__main__":
    sys.exit(main())
