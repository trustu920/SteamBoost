"""阶段 2 引擎验证：FastCopy 差异/镜像复制、进度回调、校验语义、取消、robocopy 回退。

用**隔离的合成目录**，不碰任何真实 Steam 数据。

关键语义（由本测试驱动定型）：
  * 差异复制后校验用 ``allow_extra=True``：只要求"源里的文件都完整到达"；
  * 镜像回写后校验用 ``allow_extra=False``：两侧必须完全一致，多一个文件也算失败。

运行： python tests/test_copy_engine.py
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path

# 日志重定向到测试沙箱（必须在导入应用模块之前设置，避免污染真实审计日志）
os.environ["STEAMBOOST_LOG_DIR"] = str(Path(__file__).resolve().parent / "_engine_sandbox" / "logs")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import copy_engine  # noqa: E402
from config import Config  # noqa: E402
from copy_engine import (  # noqa: E402
    MODE_DIFF,
    MODE_MIRROR,
    CopyCancelled,
    CopyFailed,
    FastCopyEngine,
    RobocopyEngine,
    detect_engine,
    engine_summary,
    fastcopy_available,
    fastcopy_candidate_dirs,
    find_fastcopy,
    parse_fastcopy_log,
    path_from_registry_value,
    plan_extras,
    registry_fastcopy_dirs,
    scan_tree,
    verify_trees,
)

SANDBOX = Path(__file__).resolve().parent / "_engine_sandbox"
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f"  → {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def build_fixture(root: Path, payload_mb: int = 0, many_files: int = 0) -> tuple[Path, Path]:
    """构造：源与目标有相同文件（跳过）、有内容不同的文件（覆盖）、目标有多余文件（镜像时删除）。"""
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    src, dst = root / "src", root / "dst"
    (src / "bin").mkdir(parents=True)
    dst.mkdir(parents=True)
    (src / "small.txt").write_bytes(b"s" * 128)
    (src / "bin" / "data.bin").write_bytes(b"d" * 4096)
    (src / "bin" / "changed.bin").write_bytes(b"n" * 2048)
    if payload_mb:
        (src / "big.bin").write_bytes(b"\0" * (payload_mb * 1024 * 1024))
    if many_files:
        bulk = src / "bulk"
        bulk.mkdir()
        for index in range(many_files):
            (bulk / f"f{index:05d}.dat").write_bytes(b"z" * 4096)

    shutil.copy2(src / "small.txt", dst / "small.txt")           # 一致 → 应跳过
    (dst / "bin").mkdir(exist_ok=True)
    (dst / "bin" / "changed.bin").write_bytes(b"o" * 999)        # 不同 → 应覆盖
    (dst / "obsolete.txt").write_bytes(b"x" * 77)                # 源没有 → 镜像时删除
    return src, dst


def left_over_logs(root: Path) -> list[str]:
    return [str(p) for p in root.rglob(".steamboot_fastcopy_*.log")]


def test_parse_log() -> None:
    """用实测得到的真实日志格式验证解析器。"""
    text = """=================================================
fcp(ver5.12.0) start at 2026/10/07 17:00:23
<Source>  C:\\tools\\src
<DestDir> C:\\tools\\dst
<Command> 差异（大小/日期） (with Verify)
<FileLog> C:\\tools\\FastCopy\\Log\\20261007-170023-0.log
-------------------------------------------------
 No Errors

TotalRead  = 0.0 MiB
TotalWrite = 12,345 MiB
TotalFiles = 2 (1)
TotalSkip  = 0.0 MiB
SkipFiles  = 1 (0)
TotalDel   = 3.0 MiB
DelFiles   = 4 (2)
TotalTime  = 0.0 sec
TransRate  = 1.00 MB/s

Result : (ErrFiles : 0 / ErrDirs : 0) at 2026/10/07 17:00:23
"""
    info = parse_fastcopy_log(text)
    check("解析 TotalFiles", info.get("TotalFiles") == 2, str(info))
    check("解析 SkipFiles", info.get("SkipFiles") == 1, str(info))
    check("解析 DelFiles", info.get("DelFiles") == 4, str(info))
    check("解析错误数", info.get("err_files") == 0, str(info))
    check(
        "解析写入字节",
        copy_engine._fc_size_to_bytes(str(info.get("TotalWrite"))) == 12345 * 1024 ** 2,
        str(info.get("TotalWrite")),
    )


def test_detection() -> None:
    """FastCopy 检测：只依赖系统自身信息（注册表／环境变量／PATH），不写死任何路径。"""
    dirs = fastcopy_candidate_dirs()
    check("候选目录非空", len(dirs) > 0, str(dirs))
    check("候选目录都是绝对路径", all(path.is_absolute() for path in dirs), str(dirs))
    check("候选目录不重复", len(dirs) == len(set(dirs)), str(dirs))
    check("注册表查询不抛异常且返回目录列表", all(isinstance(p, Path) for p in registry_fastcopy_dirs()))

    found = find_fastcopy()
    if found is not None:
        check("显式路径优先于自动查找", find_fastcopy(str(found)) == found, str(find_fastcopy(str(found))))
        check("显式路径给错时仍能自动找到", find_fastcopy(r"Q:\nope\fcp.exe") == found)
    check("不存在的盘符不会凭空找到 FastCopy", find_fastcopy(r"Q:\nope\fcp.exe") == found)

    program_files = Path(r"C:\Program Files\FastCopy")
    check("解析 InstallLocation", path_from_registry_value(r"C:\Program Files\FastCopy") == program_files)
    check(
        "解析 DisplayIcon 的 ,0 后缀",
        path_from_registry_value(r"C:\Program Files\FastCopy\FastCopy.exe,0") == program_files,
    )
    check(
        "解析带参数的 UninstallString",
        path_from_registry_value('"C:\\Program Files\\FastCopy\\unins000.exe" /SILENT') == program_files,
    )
    check("空注册表值返回 None", path_from_registry_value("") is None)
    check("只有文件名（相对路径）时返回 None", path_from_registry_value("fcp.exe") is None)

    summary = engine_summary(Config())
    if found is not None:
        check("摘要写明用的是 FastCopy", summary.startswith("FastCopy（"), summary)
    else:
        check("摘要写明已回退 robocopy", "回退" in summary, summary)
    robocopy_summary = engine_summary(Config(copy_engine="robocopy"))
    check(
        "明确选 robocopy 时说明是它、且不提示回退",
        robocopy_summary.startswith("robocopy") and "回退" not in robocopy_summary,
        robocopy_summary,
    )
    check("未知引擎名给出明确说明", "不可用" in engine_summary(Config(copy_engine="不存在的引擎")))
    check("fastcopy_available 与检测结果一致", fastcopy_available(Config()) == (found is not None))


def wait_for_child_process(name_keywords: tuple[str, ...], timeout: float = 15.0) -> bool:
    """等待本进程出现某个子进程（用于确定"复制确实已经跑起来了"）。"""
    try:
        import psutil
    except ImportError:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for child in psutil.Process().children(recursive=True):
                if any(key in child.name().lower() for key in name_keywords):
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.02)
    return False


def test_cancel(fc: FastCopyEngine, san: Path) -> None:
    """取消必须真的生效，而且要覆盖两种时机：开始前、进行中。"""
    # (a) 开始前取消：必须一次文件都不动
    src_a, dst_a = build_fixture(san / "cancel_pre", payload_mb=8)
    before = sorted(p.name for p in dst_a.iterdir())
    event = threading.Event()
    event.set()
    try:
        fc.copy_tree(src_a, dst_a, mode=MODE_DIFF, cancel_event=event, total=scan_tree(src_a))
        check("开始前取消：抛出 CopyCancelled", False, "未抛出异常")
    except CopyCancelled:
        check("开始前取消：抛出 CopyCancelled", True)
    check("开始前取消：没有落盘任何新文件", sorted(p.name for p in dst_a.iterdir()) == before)

    # (b) 进行中取消：等子进程真的起来后再取消
    src_b, dst_b = build_fixture(san / "cancel_mid", many_files=4000)
    event_b = threading.Event()
    outcome: dict[str, str] = {}

    def worker() -> None:
        try:
            fc.copy_tree(src_b, dst_b, mode=MODE_DIFF, cancel_event=event_b, total=scan_tree(src_b))
            outcome["result"] = "completed"
        except CopyCancelled:
            outcome["result"] = "cancelled"
        except Exception as exc:  # noqa: BLE001
            outcome["result"] = f"error: {exc}"

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    started = wait_for_child_process(("fcp", "fastcopy"))
    if started:
        event_b.set()
    thread.join(timeout=60)
    check("进行中取消：确实抓到了复制进程", started)
    check("进行中取消：抛出 CopyCancelled", outcome.get("result") == "cancelled", str(outcome))
    check("取消后没有残留临时日志", not left_over_logs(san / "cancel_mid"), str(left_over_logs(san / "cancel_mid")))


def main() -> int:
    cfg = Config()
    test_detection()
    fc_path = find_fastcopy()
    print(f"FastCopy：{fc_path or '（未找到）'}")
    engine = detect_engine(cfg)
    check("默认引擎是 FastCopy", isinstance(engine, FastCopyEngine), type(engine).__name__)

    if fc_path is not None:
        test_parse_log()
        san = SANDBOX
        fc = FastCopyEngine(cfg, fc_path)

        # ---------------- 差异复制 ----------------
        src, dst = build_fixture(san / "diff")
        total = scan_tree(src)
        events: list[copy_engine.ProgressState] = []
        old_interval = copy_engine.POLL_INTERVAL
        copy_engine.POLL_INTERVAL = 0.05
        try:
            result = fc.copy_tree(src, dst, mode=MODE_DIFF, on_progress=events.append, total=total)
        finally:
            copy_engine.POLL_INTERVAL = old_interval

        check("差异复制成功", result.ok, result.detail)
        check("覆盖了内容不同的文件", (dst / "bin" / "changed.bin").stat().st_size == 2048)
        check("复制了缺失的子目录文件", (dst / "bin" / "data.bin").exists())
        check("差异模式保留目标多余文件", (dst / "obsolete.txt").exists())
        check("结果里解析出文件数", result.files_copied >= 2, str(result.files_copied))
        check("进度回调被触发", len(events) > 0, f"{len(events)} 次")
        if events:
            check("进度事件带有总字节", events[-1].bytes_total == total.bytes, f"{events[-1].bytes_total} vs {total.bytes}")
        check("运行后没有残留临时日志", not left_over_logs(san / "diff"), str(left_over_logs(san / "diff")))

        # ---------------- 校验语义 ----------------
        ver = verify_trees(src, dst)
        check("差异复制后：宽松校验通过（源文件都已到达）", ver.ok, ver.detail)
        ver_strict = verify_trees(src, dst, allow_extra=False)
        check("差异复制后：严格校验因目标多出文件而不通过", not ver_strict.ok, ver_strict.detail)

        (dst / "small.txt").write_bytes(b"S" * 128)
        check("篡改文件后校验不通过", not verify_trees(src, dst).ok)

        (dst / "bin" / "data.bin").unlink()
        ver3 = verify_trees(src, dst)
        check("缺文件时校验不通过", not ver3.ok)
        check("缺失清单非空", len(ver3.missing) >= 1, str(ver3.missing))

        # ---------------- 镜像前的删除清单 ----------------
        extras, extra_bytes = plan_extras(src, dst)
        check("删除清单包含目标多余文件", "obsolete.txt" in extras, str(extras))
        check("删除清单统计字节", extra_bytes >= 77, str(extra_bytes))

        # ---------------- 镜像回写 ----------------
        result2 = fc.copy_tree(src, dst, mode=MODE_MIRROR, total=scan_tree(src))
        check("镜像复制成功", result2.ok, result2.detail)
        check("镜像删除了目标多余文件", not (dst / "obsolete.txt").exists())
        check("镜像补齐了缺失文件", (dst / "bin" / "data.bin").exists())
        ver4 = verify_trees(src, dst, allow_extra=False)
        check("镜像后严格校验通过", ver4.ok, ver4.detail)

        # ---------------- 取消 ----------------
        test_cancel(fc, san)

        # ---------------- 缺失引擎必须明确失败 ----------------
        try:
            FastCopyEngine(cfg, san / "not_exist_fcp.exe")
            check("缺失的 FastCopy 立即失败", False, "没有抛异常")
        except CopyFailed:
            check("缺失的 FastCopy 立即失败", True)

    # ---------------- robocopy 回退 ----------------
    src_r, dst_r = build_fixture(SANDBOX / "robocopy")
    robo = detect_engine(cfg, prefer="robocopy")
    check("可显式选择 robocopy", isinstance(robo, RobocopyEngine), type(robo).__name__)
    result3 = robo.copy_tree(src_r, dst_r, mode=MODE_DIFF, total=scan_tree(src_r))
    check("robocopy 差异复制成功", result3.ok, result3.detail)
    check("robocopy 复制了文件", (dst_r / "bin" / "data.bin").exists())
    check("robocopy 差异模式保留多余文件", (dst_r / "obsolete.txt").exists())
    check("robocopy 结果宽松校验通过", verify_trees(src_r, dst_r).ok)

    result4 = robo.copy_tree(src_r, dst_r, mode=MODE_MIRROR, total=scan_tree(src_r))
    check("robocopy 镜像复制成功", result4.ok, result4.detail)
    check("robocopy 镜像删除多余文件", not (dst_r / "obsolete.txt").exists())
    check("robocopy 镜像后严格校验通过", verify_trees(src_r, dst_r, allow_extra=False).ok)

    # 收尾：先关日志句柄再删沙箱（否则打开着的日志文件在 Windows 上删不掉）
    from logger import close_loggers

    close_loggers()
    shutil.rmtree(SANDBOX, ignore_errors=True)
    check("沙箱已清理", not SANDBOX.exists())
    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
