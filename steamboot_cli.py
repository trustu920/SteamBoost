"""SteamBoost 命令行：加速 / 回写 / 释放 / 隔离区管理（阶段 3 验证入口）。

用法
----
::

    python steamboot_cli.py scan                       # 扫描并列出游戏与状态
    python steamboot_cli.py accelerate <appid>         # 加速到 SSD
    python steamboot_cli.py writeback <appid>          # 回写母盘（保持加速）
    python steamboot_cli.py release <appid>            # 释放 SSD 空间
    python steamboot_cli.py quarantine list            # 查看隔离区
    python steamboot_cli.py quarantine purge <目录>     # 删除隔离项（需输入确认词）
    python steamboot_cli.py quarantine restore <目录>   # 还原隔离项

删除类动作一律**交互式确认**：程序会把要删的目录、文件数、字节数打印出来，
只有你亲手输入确认词「删除」才会执行；其他任何输入都取消。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from config import Config, drive_of, human_size, normalize_drive
from deletion_guard import PromptConfirmer, allowed_roots
from logger import setup_logger
from operations import (
    AutoExtrasConfirmer,
    OperationBlocked,
    OperationFailed,
    accelerate,
    release,
    writeback,
)
from quarantine import list_items, purge_item, restore_item
from state import StateStore
from steam_scanner import ACCELERATABLE, RELEASABLE, scan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SteamBoost 命令行（阶段 3）")
    parser.add_argument("--mother", default="", help="母盘盘符，例如 F")
    parser.add_argument("--cache", dest="cache_drive", default="", help="加速盘盘符，例如 D")
    parser.add_argument("--cache-dir", default="", help="缓存目录")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="扫描并列出游戏")

    for name, help_text in (
        ("accelerate", "加速到 SSD"),
        ("writeback", "回写母盘（保持加速状态）"),
        ("release", "释放 SSD 空间"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("appid", help="Steam appid")
        item.add_argument("--dry-run", action="store_true", help="只做前置检查，不执行")

    quarantine = sub.add_parser("quarantine", help="隔离区管理")
    quarantine.add_argument("action", choices=["list", "purge", "restore"])
    quarantine.add_argument("item", nargs="?", help="隔离项目录")
    return parser


def load_config(args) -> Config:
    cfg = Config.load()
    if args.mother:
        cfg.mother_drive = normalize_drive(args.mother)
    if args.cache_drive:
        cfg.cache_drive = normalize_drive(args.cache_drive)
    if args.cache_dir:
        cfg.cache_dir = args.cache_dir
    if not cfg.cache_dir and cfg.cache_drive:
        cfg.cache_dir = str(cfg.resolved_cache_dir())
    return cfg


def find_game(cfg: Config, appid: str):
    store = StateStore(cfg=cfg)
    report = scan(cfg, state=store.to_dict())
    matches = report.find(appid)
    if not matches:
        raise SystemExit(f"未找到 appid={appid} 的游戏")
    if len(matches) > 1:
        print(f"提醒：appid {appid} 在 {len(matches)} 个库中出现，使用第一个：{matches[0].library_path}")
    return matches[0], store, report


def progress_printer():
    """把进度回调变成一行滚动显示。"""
    state = {"last": 0.0}

    def callback(progress) -> None:
        import time as _time

        now = _time.time()
        if now - state["last"] < 0.5 and progress.percent < 100:
            return
        state["last"] = now
        print(
            f"\r  进度 {progress.percent:5.1f}%  "
            f"{human_size(progress.bytes_done)} / {human_size(progress.bytes_total)}  "
            f"{progress.speed_text}  ETA {progress.eta_text}",
            end="",
            flush=True,
        )
        if progress.percent >= 100:
            print()

    return callback


def report_blocked(exc: OperationBlocked) -> int:
    print("\n操作已中止（未改动任何文件）：")
    for blocker in exc.blockers:
        print(f"  - {blocker}")
    return 1


def cmd_scan(cfg: Config) -> int:
    store = StateStore(cfg=cfg)
    report = scan(cfg, state=store.to_dict())
    print(f"母盘 {cfg.mother_drive or '未选'} / 加速盘 {cfg.cache_drive or '未选'} / 缓存 {cfg.cache_dir}")
    print(f"共 {len(report.games)} 款游戏\n")
    for game in report.games:
        flags = []
        if game.status in ACCELERATABLE:
            flags.append("可加速")
        if game.status in RELEASABLE:
            flags.append("可回写/释放")
        print(
            f"  {game.appid:<9}{game.name[:34]:<36}{human_size(game.size_on_disk):>11}  "
            f"{game.status_label:<10}{', '.join(flags)}"
        )
    if report.quarantine_items:
        print("\n隔离区（等你确认后删除）：")
        for item in report.quarantine_items:
            print(f"  {item.created_text}  {item.appid} {item.name}  {item.files} 文件 / {item.size_text}")
            print(f"      {item.item_dir}")
    if report.anomalies:
        print("\n自检提示：")
        for anomaly in report.anomalies:
            print(f"  [{anomaly.level}] {anomaly.kind}: {anomaly.message}")
    return 0


def cmd_operate(cfg: Config, args) -> int:
    game, store, _report = find_game(cfg, args.appid)
    action = args.command

    if args.dry_run:
        print(f"（演练模式）{game.name} 当前状态：{game.status_label}")
        return 0

    confirmer = PromptConfirmer(word="删除")
    extras = AutoExtrasConfirmer()
    progress = progress_printer()

    try:
        if action == "accelerate":
            result = accelerate(
                game, cfg, store=store, on_progress=progress,
                deletion_confirmer=confirmer,
            )
        elif action == "writeback":
            result = writeback(game, cfg, store=store, on_progress=progress, extras_confirmer=extras)
        else:
            result = release(
                game, cfg, store=store, on_progress=progress,
                deletion_confirmer=confirmer, extras_confirmer=extras,
            )
    except OperationBlocked as exc:
        return report_blocked(exc)
    except OperationFailed as exc:
        print(f"\n操作失败：{exc}")
        if exc.detail:
            print(f"  现场状态：{exc.detail}")
        return 1

    print(f"\n完成：{result.message}")
    if result.detail:
        print(f"  {result.detail}")
    return 0


def cmd_quarantine(cfg: Config, args) -> int:
    if args.action == "list":
        items = list_items(cfg.mother_drive)
        if not items:
            print("隔离区为空")
            return 0
        for item in items:
            print(f"{item.created_text}  {item.appid} {item.name}  {item.files} 文件 / {item.size_text}")
            print(f"    {item.item_dir}")
        return 0

    if not args.item:
        raise SystemExit("请提供隔离项目录")
    if args.action == "purge":
        freed = purge_item(args.item, cfg.mother_drive, PromptConfirmer(word="删除"))
        print(f"已删除，释放 {human_size(freed)}")
        return 0

    restored = restore_item(args.item, cfg.mother_drive)
    print(f"已还原 {restored} 个文件")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args)
    setup_logger("steamboot.cli", cfg)
    if args.command == "scan":
        return cmd_scan(cfg)
    if args.command == "quarantine":
        return cmd_quarantine(cfg, args)
    return cmd_operate(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
