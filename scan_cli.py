"""阶段 1 的命令行验证入口：扫描 Steam 库并打印游戏清单、异常与隔离区。

用法：
    python scan_cli.py                 # 表格输出
    python scan_cli.py --json          # JSON 输出（便于自动化比对）
    python scan_cli.py --volumes       # 顺带打印盘符列表（设置页会用同一份数据）

两个盘的默认值（取不到就沿用配置文件里的值）：
    --mother F      指定母盘（存游戏母本），盘符按本机实际情况填写
    --cache D       指定加速盘（存 SSD 缓存副本），需与母盘不同
    --cache-dir <加速盘>:\\SteamBoostCache
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from config import Config, drive_of, human_size, normalize_drive
from logger import setup_logger
from steam_scanner import scan, scan_summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SteamBoost 扫描器（阶段 1）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--volumes", action="store_true", help="打印盘符列表")
    parser.add_argument("--mother", default="", help="母盘盘符，例如 F")
    parser.add_argument("--cache", dest="cache_drive", default="", help="加速盘盘符，例如 D")
    parser.add_argument("--cache-dir", default="", help="缓存目录（不写入配置文件）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load()
    if args.mother or args.cache_drive or args.cache_dir:
        cfg.mother_drive = normalize_drive(args.mother) or cfg.mother_drive
        cfg.cache_drive = normalize_drive(args.cache_drive) or cfg.cache_drive
        cfg.cache_dir = args.cache_dir or cfg.cache_dir
    log = setup_logger("steamboot.cli", cfg)

    report = scan(cfg)
    log.info(
        "扫描完成：Steam=%s 游戏=%d 库=%d 异常=%d",
        report.steam_root,
        len(report.games),
        len(report.libraries),
        len(report.anomalies),
    )
    for item in report.anomalies:
        (log.error if item.level == "error" else log.warning)("自检 %s：%s", item.kind, item.message)

    libraries = [(lib.path, drive_of(lib.path)) for lib in report.libraries]
    problems, warnings = cfg.validate(libraries)

    if args.json:
        payload = {
            "steam_root": report.steam_root,
            "mother_drive": cfg.mother_drive,
            "cache_drive": cfg.cache_drive,
            "cache_dir": report.cache_dir,
            "cache_dir_configured": report.cache_dir_configured,
            "libraries": [asdict(lib) for lib in report.libraries],
            "games": [asdict(game) for game in report.games],
            "anomalies": [asdict(item) for item in report.anomalies],
            "volumes": report.volumes,
            "quarantine": [asdict(item) for item in report.quarantine_items],
            "problems": problems,
            "warnings": warnings,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    line = "=" * 100
    print(line)
    print(f"母盘（母本）：{cfg.mother_drive or '未选择'}    加速盘（缓存）：{cfg.cache_drive or '未选择'}")
    print(scan_summary(report))
    print(line)

    if args.volumes:
        print("\n盘符列表：")
        for vol in report.volumes:
            role = cfg.role_of_drive(vol["drive"])
            label = {"mother": "母盘", "cache": "加速盘"}.get(role or "", "—")
            print(
                f"  {vol['drive']}: 总 {human_size(vol['total']):>10}  "
                f"可用 {human_size(vol['free']):>10}  角色：{label}"
            )

    print("\n库：")
    for lib in report.libraries:
        state = "可用" if lib.exists else "**路径不存在**"
        drive = drive_of(lib.path)
        role = {"mother": "母盘", "cache": "加速盘"}.get(cfg.role_of_drive(drive) or "", "不在选定盘")
        print(f"  [{state}] {lib.path}   登记项目 {len(lib.apps)} 个   （{role}）")

    print("\n游戏清单：")
    header = f"{'appid':<10}{'名称':<36}{'大小':>12}  {'状态':<12}{'最近游玩':<18}库"
    print(header)
    print("-" * len(header))
    for game in report.games:
        name = game.name if len(game.name) <= 34 else game.name[:33] + "…"
        print(
            f"{game.appid:<10}{name:<36}{human_size(game.size_on_disk):>12}  "
            f"{game.status_label:<12}{game.last_played_text:<18}{game.library_path}"
        )
        for note in game.notes:
            print(f"{'':<10}↳ {note}")

    if report.quarantine_items:
        print("\n隔离区（等你确认后才删除）：")
        for item in report.quarantine_items:
            print(f"  {item.created_text}  {item.appid} {item.name}  {item.files} 个文件 / {item.size_text}")
            print(f"      {item.item_dir}")

    if report.anomalies:
        print("\n异常与自检提示：")
        for item in report.anomalies:
            flag = "错误" if item.level == "error" else "提醒"
            print(f"  [{flag}] {item.kind}: {item.message}")

    if problems or warnings:
        print("\n配置校验（只针对所选两个盘上的 Steam 库）：")
        for text in problems:
            print(f"  [致命] {text}")
        for text in warnings:
            print(f"  [提醒] {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
