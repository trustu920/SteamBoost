"""阶段 1 验证：用「合成库布局」检查扫描器的状态机，全程不触碰真实 Steam。

覆盖分支：
  1. 真实目录 + 盘符角色 → HDD 中 / 已在 SSD
  2. 原位置是 Junction 且指向缓存副本 → 已加速
  3. 原位置缺失但 .hdd_cache 有母本 → 加速中断
  4. 清单在但目录和备份都没有 → 目录缺失（ACF 残留）
  5. common 下没有清单的残留目录 → 自检异常
  6. libraryfolders.vdf 里已失效的库路径 → 自检异常
  7. 缓存目录落在 Steam 库内部 → 必须被判定为致命问题

运行： python tests/test_scanner_fixture.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# 日志重定向到测试沙箱（必须在导入应用模块之前设置，避免污染真实审计日志）
os.environ["STEAMBOOST_LOG_DIR"] = str(Path(__file__).resolve().parent / "_fixtures" / "logs")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config, HDD_CACHE_DIR_NAME  # noqa: E402
from steam_scanner import ST_ACCELERATED, ST_ACCELERATING, ST_MISSING, ST_ON_HDD, scan  # noqa: E402

FIXTURE_ROOT = Path(__file__).resolve().parent / "_fixtures"

ACF_TEMPLATE = """\"AppState\"
{{
\t\"appid\"\t\t\"{appid}\"
\t\"name\"\t\t\"{name}\"
\t\"StateFlags\"\t\t\"4\"
\t\"installdir\"\t\t\"{installdir}\"
\t\"LastPlayed\"\t\t\"{last_played}\"
\t\"SizeOnDisk\"\t\t\"{size}\"
}}
"""

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f"  → {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def make_acf(path: Path, appid: str, name: str, installdir: str, size: int = 1024, last_played: int = 0) -> None:
    path.write_text(
        ACF_TEMPLATE.format(appid=appid, name=name, installdir=installdir, size=size, last_played=last_played),
        encoding="utf-8",
    )


def make_junction(link: Path, target: Path) -> bool:
    """用 mklink /J 创建目录联接（不需要管理员权限）。"""
    link.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def remove_junction(link: Path) -> None:
    """只删联接本身——绝不使用递归删除（那会连带删掉目标内容）。"""
    if link.exists():
        subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True, text=True)


def build_fixture() -> tuple[Path, Path]:
    if FIXTURE_ROOT.exists():
        shutil.rmtree(FIXTURE_ROOT, ignore_errors=True)
    lib = FIXTURE_ROOT / "lib_hdd"
    cache = FIXTURE_ROOT / "cache"
    steamapps = lib / "steamapps"
    common = steamapps / "common"
    common.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    # 1) 普通安装：真实目录
    (common / "PlainGame").mkdir(parents=True)
    (common / "PlainGame" / "plain.bin").write_bytes(b"x" * 2048)
    make_acf(steamapps / "appmanifest_777777.acf", "777777", "Plain Game", "PlainGame", 2048, 1700000000)

    # 2) 已加速：原位置是 Junction，指向缓存副本
    copy_dir = cache / "888888_LinkedGame"
    copy_dir.mkdir(parents=True)
    (copy_dir / "linked.bin").write_bytes(b"y" * 4096)
    make_acf(steamapps / "appmanifest_888888.acf", "888888", "Linked Game", "LinkedGame", 4096)
    make_junction(common / "LinkedGame", copy_dir)

    # 3) 加速中断：原位置缺失，.hdd_cache 里有母本
    backup = common / HDD_CACHE_DIR_NAME / "BackupGame"
    backup.mkdir(parents=True)
    (backup / "backup.bin").write_bytes(b"z" * 8192)
    make_acf(steamapps / "appmanifest_999999.acf", "999999", "Backup Game", "BackupGame", 8192)

    # 4) 清单残留：目录与备份都不存在
    make_acf(steamapps / "appmanifest_555555.acf", "555555", "Gone Game", "GoneGame", 512)

    # 5) 没有清单的残留目录
    (common / "Leftover").mkdir(parents=True)
    (common / "Leftover" / "junk.bin").write_bytes(b"j" * 128)

    # 6) libraryfolders.vdf：含一个已失效的库路径
    (steamapps / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n'
        '\t"0"\n\t{\n\t\t"path"\t\t"' + str(lib).replace("\\", "\\\\") + '"\n\t\t"label"\t\t""\n\t\t"apps"\n\t\t{\n'
        '\t\t\t"777777"\t\t"2048"\n\t\t}\n\t}\n'
        '\t"1"\n\t{\n\t\t"path"\t\t"' + str(FIXTURE_ROOT / "lib_gone").replace("\\", "\\\\") + '"\n\t\t"label"\t\t""\n\t\t"apps"\n\t\t{\n'
        '\t\t\t"123"\t\t"999"\n\t\t}\n\t}\n}\n',
        encoding="utf-8",
    )
    return lib, cache


def main() -> int:
    lib, cache = build_fixture()
    drive = os.path.splitdrive(str(lib))[0].rstrip(":\\").upper()

    cfg = Config()
    cfg.cache_dir = str(cache)
    cfg.mother_drive = drive        # 合成环境里库在这个盘上 → 该盘作母盘
    cfg.cache_drive = "Q"           # 加速盘用一个不存在的盘符，避免与母盘相同

    report = scan(cfg, steam_root=str(lib))
    by_appid = {g.appid: g for g in report.games}
    kinds = {a.kind for a in report.anomalies}

    check("解析出 4 个游戏", len(report.games) == 4, f"实际 {len(report.games)}：{[g.appid for g in report.games]}")
    check(
        "777777 真实目录 → HDD 中",
        by_appid.get("777777") is not None and by_appid["777777"].status == ST_ON_HDD,
        str(by_appid.get("777777").status if by_appid.get("777777") else None),
    )
    linked = by_appid.get("888888")
    check(
        "888888 Junction → 已加速",
        linked is not None and linked.status == ST_ACCELERATED,
        f"{linked.status if linked else None} notes={linked.notes if linked else None}",
    )
    check(
        "888888 联接目标解析正确",
        linked is not None and os.path.normcase(linked.junction_target) == os.path.normcase(str(cache / "888888_LinkedGame")),
        linked.junction_target if linked else "",
    )
    backup_game = by_appid.get("999999")
    check(
        "999999 .hdd_cache 母本 → 加速中断",
        backup_game is not None and backup_game.status == ST_ACCELERATING,
        str(backup_game.status if backup_game else None),
    )
    gone = by_appid.get("555555")
    check("555555 无目录无备份 → 目录缺失", gone is not None and gone.status == ST_MISSING, str(gone.status if gone else None))

    # 回归用例：显示名（"Plain Game"）与安装目录名（"PlainGame"）不同时，
    # 路径必须由 ACF 的 installdir 解析——绝不能拿游戏名去拼路径。
    plain = by_appid.get("777777")
    check(
        "路径按 installdir 解析而非显示名",
        plain is not None
        and os.path.normcase(Path(plain.game_path).name) == os.path.normcase("PlainGame")
        and plain.name == "Plain Game",
        f"{plain.game_path if plain else None} / {plain.name if plain else None}",
    )
    check("自检发现 acf_without_files", "acf_without_files" in kinds, str(sorted(kinds)))
    check("自检发现 unmanaged_folder", "unmanaged_folder" in kinds, str(sorted(kinds)))
    check("自检发现 stale_library", "stale_library" in kinds, str(sorted(kinds)))

    # 组合校验：新的"两个盘"模型
    cfg_bad = Config(cache_dir=str(lib / "cache_inside"), mother_drive=drive, cache_drive="Q")
    problems, _ = cfg_bad.validate([(str(lib), drive)])
    check("库内缓存目录被判致命问题", any("库内部" in p for p in problems), str(problems))

    cfg_ok = Config(cache_dir=r"Q:\SteamBoostCache", mother_drive=drive, cache_drive="Q")
    problems_ok, _ = cfg_ok.validate([(str(lib), drive)])
    check("正常配置无致命问题", not problems_ok, str(problems_ok))

    cfg_same = Config(cache_dir=r"Q:\c", mother_drive=drive, cache_drive=drive)
    problems_same, _ = cfg_same.validate([(str(lib), drive)])
    check("母盘与加速盘相同被判致命问题", any("同一个盘" in p for p in problems_same), str(problems_same))

    cfg_no_lib = Config(cache_dir=r"Q:\c", mother_drive="X", cache_drive="Q")
    problems_no_lib, _ = cfg_no_lib.validate([(str(lib), drive)])
    check("母盘上没有 Steam 库被判致命问题", any("没有找到任何 Steam 库" in p for p in problems_no_lib), str(problems_no_lib))

    # 清理：先删联接本身，再删整个 fixture 树
    remove_junction(Path(by_appid["888888"].game_path))
    check("联接删除后目标内容仍在", (cache / "888888_LinkedGame" / "linked.bin").exists())
    from logger import close_loggers

    close_loggers()
    shutil.rmtree(FIXTURE_ROOT, ignore_errors=True)
    check("测试夹具已清理", not FIXTURE_ROOT.exists())

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
