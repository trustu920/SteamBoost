"""Steam 库扫描器：注册表 → libraryfolders.vdf → appmanifest_*.acf → 文件系统事实。

关键设计（来自对真实环境的勘察，不是假想）：

1. ``libraryfolders.vdf`` 里的库条目可能已经失效（盘符被改或目录被删），
   因此每条库记录都必须验证 ``路径存在``，失效库只报异常、不参与后续操作。
2. 同一个 appid 可能与两个库的 ``appmanifest`` 同时存在（真实出现过），
   因此唯一键是 ``(库路径, appid)`` 而不是 appid。
3. 不同库可能出现同名 ``installdir``（真实出现过），
   所以绝不能只用目录名判断游戏身份。
4. ACF 只提供元数据（appid/名称/installdir/SizeOnDisk/LastPlayed/StateFlags），
   **当前状态必须以文件系统事实为准**：目录是否 Junction、``.hdd_cache`` 是否有母本、
   SSD 缓存副本是否存在。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("steamboot.scanner")

from config import (
    CACHE_DIR_NAME,
    HDD_CACHE_DIR_NAME,
    Config,
    drive_of,
    list_volumes,
    trash_root,
)
from junction_utils import is_junction, junction_target, strip_long_prefix  # noqa: F401  (统一实现在 junction_utils)
from quarantine import list_items as quarantine_items

# --------------------------------------------------------------------- 状态
ST_ON_HDD = "on_hdd"              # 母本在所选母盘上，未加速
ST_ON_SSD = "on_ssd"              # 游戏本身就装在加速盘上的 Steam 库
ST_ON_OTHER = "on_other"          # 所在盘符不是所选母盘/加速盘
ST_ACCELERATED = "accelerated"    # 已加速：Junction 指向 SSD 缓存副本
ST_ACCELERATING = "accelerating"  # 加速中断：母本在 .hdd_cache，副本可能不完整
ST_WRITING_BACK = "writing_back"  # 回写中断
ST_MISSING = "missing"            # ACF 在，但游戏目录不存在（且无 .hdd_cache 母本）
ST_UNKNOWN = "unknown"

STATUS_LABELS = {
    ST_ON_HDD: "母盘中",
    ST_ON_SSD: "已在加速盘",
    ST_ON_OTHER: "不在选定盘",
    ST_ACCELERATED: "已加速",
    ST_ACCELERATING: "加速中断",
    ST_WRITING_BACK: "回写中断",
    ST_MISSING: "目录缺失",
    ST_UNKNOWN: "未知",
}

#: 允许加速的状态：只有位于所选母盘上的游戏才谈得上"加速"
ACCELERATABLE = {ST_ON_HDD}
#: 允许回写/释放的状态
RELEASABLE = {ST_ACCELERATED, ST_WRITING_BACK}

#: common 下由 Steam 自己维护、没有清单也不代表残留的目录（避免自检噪声）
IGNORED_COMMON_DIRS = {"steam controller configs"}


# ---------------------------------------------------------------- 数据结构
@dataclass
class SteamLibrary:
    """一个 Steam 库（steamapps 的父目录）。"""

    path: str
    label: str = ""
    apps: dict[str, int] = field(default_factory=dict)  # vdf 中登记的 appid → 占用字节
    exists: bool = False
    source: str = "libraryfolders.vdf"

    @property
    def steamapps(self) -> Path:
        return Path(self.path) / "steamapps"

    @property
    def common(self) -> Path:
        return self.steamapps / "common"

    @property
    def hdd_cache(self) -> Path:
        return self.common / HDD_CACHE_DIR_NAME


@dataclass
class GameRecord:
    """一款游戏在某个库里的记录（唯一键 = 库 + appid）。"""

    appid: str
    name: str
    installdir: str
    library_path: str
    size_on_disk: int = 0
    last_played: int = 0
    state_flags: int = 0
    acf_path: str = ""

    game_path: str = ""
    hdd_backup_path: str = ""
    cache_copy_path: str = ""
    status: str = ST_UNKNOWN
    junction_target: str = ""
    cache_copy_exists: bool = False
    hdd_backup_exists: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def is_accelerated(self) -> bool:
        return self.status == ST_ACCELERATED

    @property
    def size_text(self) -> str:
        return human_size(self.size_on_disk)

    @property
    def last_played_text(self) -> str:
        if not self.last_played:
            return "从未"
        import datetime as _dt

        return _dt.datetime.fromtimestamp(self.last_played).strftime("%Y-%m-%d %H:%M")


@dataclass
class Anomaly:
    """自检发现的异常，供修复向导使用。"""

    kind: str          # stale_library / missing_game_dir / orphan_backup / duplicate_appid ...
    level: str         # error / warning
    message: str
    path: str = ""
    appid: str = ""


@dataclass
class ScanReport:
    steam_root: str = ""
    libraries: list[SteamLibrary] = field(default_factory=list)
    games: list[GameRecord] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    volumes: list[dict[str, Any]] = field(default_factory=list)
    cache_dir: str = ""
    cache_dir_configured: bool = False
    quarantine_items: list[Any] = field(default_factory=list)

    def games_by_status(self) -> dict[str, list[GameRecord]]:
        grouped: dict[str, list[GameRecord]] = {}
        for game in self.games:
            grouped.setdefault(game.status, []).append(game)
        return grouped

    def find(self, appid: str | int) -> list[GameRecord]:
        key = str(appid)
        return [g for g in self.games if g.appid == key]


# ------------------------------------------------------------------ 小工具
def human_size(num_bytes: int | float | None) -> str:
    """把字节数格式化成人类可读文本。"""
    value = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TB"


# 说明：is_junction / junction_target / strip_long_prefix 的实现统一放在 junction_utils，
# 这里只是通过顶部 import 再导出，保证"删除联接"这类危险操作只有一个实现点。


# ------------------------------------------------------------ VDF/ACF 解析
def _tokenize_vdf(text: str) -> list[str]:
    """把 VDF 文本切成 token：``{`` / ``}`` / 字符串。支持 ``//`` 注释与转义。"""
    tokens: list[str] = []
    i, n = 0, len(text)
    escapes = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}
    while i < n:
        ch = text[i]
        if ch in " \t\r\n\ufeff":
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch in "{}":
            tokens.append(ch)
            i += 1
            continue
        if ch == '"':
            i += 1
            buf: list[str] = []
            while i < n:
                cur = text[i]
                if cur == "\\" and i + 1 < n:
                    buf.append(escapes.get(text[i + 1], text[i + 1]))
                    i += 2
                    continue
                if cur == '"':
                    i += 1
                    break
                buf.append(cur)
                i += 1
            tokens.append("".join(buf))
            continue
        j = i
        while j < n and text[j] not in " \t\r\n{}":
            j += 1
        tokens.append(text[i:j])
        i = j
    return tokens


def parse_vdf(text: str) -> dict[str, Any]:
    """解析 VDF 文本为嵌套 dict（只覆盖 Steam 实际使用的语法）。"""
    tokens = _tokenize_vdf(text)
    pos = 0

    def parse_block() -> dict[str, Any]:
        nonlocal pos
        block: dict[str, Any] = {}
        while pos < len(tokens):
            token = tokens[pos]
            if token == "}":
                pos += 1
                return block
            key = token
            pos += 1
            if pos >= len(tokens):
                break
            value = tokens[pos]
            if value == "{":
                pos += 1
                block[key] = parse_block()
            else:
                block[key] = value
                pos += 1
        return block

    root: dict[str, Any] = {}
    while pos < len(tokens):
        token = tokens[pos]
        if token == "}":
            pos += 1
            continue
        key = token
        pos += 1
        if pos < len(tokens) and tokens[pos] == "{":
            pos += 1
            root[key] = parse_block()
        elif pos < len(tokens):
            root[key] = tokens[pos]
            pos += 1
    return root


def read_vdf_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    """读取并解析 VDF/ACF 文件；失败时返回空 dict（调用方负责报异常）。"""
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    return parse_vdf(text)


def read_acf(path: str | os.PathLike[str]) -> dict[str, Any]:
    """读取 appmanifest_*.acf，返回 ``AppState`` 字典。"""
    data = read_vdf_file(path)
    state = data.get("AppState")
    return state if isinstance(state, dict) else {}


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# -------------------------------------------------------------- 注册表读取
def find_steam_root() -> str:
    """从注册表读取 Steam 安装路径；失败时回退常见安装位置。"""
    candidates: list[str] = []
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        winreg = None  # type: ignore[assignment]

    if winreg is not None:
        probes = [
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ]
        for hive, subkey, value_name in probes:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    value, _ = winreg.QueryValueEx(key, value_name)
                if value:
                    candidates.append(str(value))
            except OSError:
                continue

    # 兜底：从系统环境变量拼出 Steam 的默认安装位置（不写死盘符）
    for variable, sub in (("ProgramFiles(x86)", "Steam"), ("ProgramFiles", "Steam"), ("SystemDrive", "")):
        base = os.environ.get(variable)
        if base:
            candidates.append(os.path.join(base, sub) if sub else os.path.join(base + os.sep, "Steam"))

    for candidate in candidates:
        root = Path(os.path.normpath(candidate))
        if (root / "steamapps").is_dir():
            return str(root)
    return str(Path(os.path.normpath(candidates[0]))) if candidates else ""


# ---------------------------------------------------------------- 库发现
def discover_libraries(steam_root: str | os.PathLike[str]) -> tuple[list[SteamLibrary], list[Anomaly]]:
    """解析 libraryfolders.vdf 得到所有 Steam 库（含失效库，供自检提示）。"""
    root = Path(steam_root)
    libraries: list[SteamLibrary] = []
    anomalies: list[Anomaly] = []
    seen: set[str] = set()

    vdf_path = root / "steamapps" / "libraryfolders.vdf"
    data = read_vdf_file(vdf_path) if vdf_path.is_file() else {}
    entries = data.get("libraryfolders") if isinstance(data.get("libraryfolders"), dict) else {}

    # 主库始终存在（即使 vdf 缺失）
    primary = os.path.normcase(os.path.abspath(str(root)))
    seen.add(primary)
    libraries.append(
        SteamLibrary(
            path=str(root),
            label="",
            apps={},
            exists=(root / "steamapps").is_dir(),
            source="steam_root",
        )
    )

    for key, value in (entries or {}).items():
        if not isinstance(value, dict):
            continue
        raw_path = value.get("path") or value.get("Path") or ""
        if not raw_path:
            continue
        norm_path = os.path.normpath(str(raw_path))
        norm_key = os.path.normcase(os.path.abspath(norm_path))
        apps = {
            str(k): to_int(v)
            for k, v in (value.get("apps") or {}).items()
            if str(v).strip().isdigit()
        }
        # 主库在 vdf 里也会出现：把登记信息合并进已创建的主库条目，而不是丢弃
        if norm_key == primary:
            libraries[0].label = str(value.get("label") or "")
            libraries[0].apps = apps
            continue
        if norm_key in seen:
            continue
        seen.add(norm_key)
        exists = Path(norm_path).is_dir()
        libraries.append(
            SteamLibrary(
                path=norm_path,
                label=str(value.get("label") or ""),
                apps=apps,
                exists=exists,
            )
        )
        if not exists:
            anomalies.append(
                Anomaly(
                    kind="stale_library",
                    level="warning",
                    message=f"libraryfolders.vdf 登记的库路径不存在：{norm_path}"
                    + (f"（vdf 内仍列有 {len(apps)} 个项目）" if apps else ""),
                    path=norm_path,
                )
            )
    return libraries, anomalies


# ---------------------------------------------------------------- 主扫描
def scan(cfg: Config | None = None, state: dict[str, Any] | None = None, steam_root: str = "") -> ScanReport:
    """执行一次完整扫描，返回库、游戏与异常清单。

    state 为阶段 3 的 ``state.json`` 内容（记录进行中的任务），
    这里只用它区分「加速中断」与「回写中断」。
    steam_root 显式指定时跳过注册表读取——单元测试与修复向导都用得到。
    """
    config = cfg or Config.load()
    root_text = steam_root or find_steam_root()
    report = ScanReport(steam_root=root_text, volumes=list_volumes())
    report.cache_dir = str(config.resolved_cache_dir())
    report.cache_dir_configured = bool(config.cache_dir.strip())

    if not root_text:
        report.anomalies.append(
            Anomaly(kind="no_steam", level="error", message="未找到 Steam 安装路径")
        )
        return report

    libraries, lib_anomalies = discover_libraries(root_text)
    report.libraries = libraries
    report.anomalies.extend(lib_anomalies)

    cache_root = config.resolved_cache_dir()
    in_progress = (state or {}).get("tasks", {}) if isinstance(state, dict) else {}
    seen_appid_library: dict[str, str] = {}
    seen_installdir: dict[str, str] = {}
    referenced_backups: set[str] = set()
    referenced_dirs: set[str] = set()

    for library in libraries:
        common = library.common
        if not library.exists:
            continue
        acf_files = sorted(library.steamapps.glob("appmanifest_*.acf"))
        for acf in acf_files:
            data = read_acf(acf)
            if not data:
                report.anomalies.append(
                    Anomaly(
                        kind="bad_acf",
                        level="warning",
                        message=f"无法解析的清单文件：{acf.name}",
                        path=str(acf),
                    )
                )
                continue

            appid = str(data.get("appid") or acf.stem.split("_")[-1])
            installdir = str(data.get("installdir") or "")
            name = str(data.get("name") or installdir or appid)
            game = GameRecord(
                appid=appid,
                name=name,
                installdir=installdir,
                library_path=library.path,
                size_on_disk=to_int(data.get("SizeOnDisk")),
                last_played=to_int(data.get("LastPlayed")),
                state_flags=to_int(data.get("StateFlags")),
                acf_path=str(acf),
                game_path=str(common / installdir) if installdir else "",
                hdd_backup_path=str(library.hdd_cache / installdir) if installdir else "",
                cache_copy_path=str(cache_root / f"{appid}_{installdir}") if installdir else "",
            )

            # ---- 重复登记检测（唯一键是 库+appid） ----
            prev_lib = seen_appid_library.get(appid)
            if prev_lib and os.path.normcase(prev_lib) != os.path.normcase(library.path):
                report.anomalies.append(
                    Anomaly(
                        kind="duplicate_appid",
                        level="warning",
                        message=f"appid {appid}（{name}）同时存在于两个库：{prev_lib} 与 {library.path}",
                        appid=appid,
                        path=library.path,
                    )
                )
            else:
                seen_appid_library[appid] = library.path

            if installdir:
                key = os.path.normcase(installdir)
                prev_dir = seen_installdir.get(key)
                if prev_dir and os.path.normcase(prev_dir) != os.path.normcase(library.path):
                    report.anomalies.append(
                        Anomaly(
                            kind="duplicate_installdir",
                            level="warning",
                            message=f"安装目录名 {installdir} 在两个库中重复：{prev_dir} 与 {library.path}",
                            path=installdir,
                        )
                    )
                else:
                    seen_installdir[key] = library.path

            _classify_game(game, config, in_progress)
            referenced_backups.add(os.path.normcase(game.hdd_backup_path))
            if game.game_path:
                referenced_dirs.add(os.path.normcase(os.path.abspath(game.game_path)))
            if game.status == ST_MISSING:
                report.anomalies.append(
                    Anomaly(
                        kind="acf_without_files",
                        level="warning",
                        message=f"清单存在但游戏目录不存在：{game.name}（{game.installdir}）",
                        appid=appid,
                        path=game.game_path,
                    )
                )
            report.games.append(game)

    # ---- common 下没有对应清单的目录（搬移/卸载残留，只报告、绝不删除） ----
    for library in libraries:
        if not library.exists:
            continue
        try:
            children = [p for p in library.common.iterdir() if p.is_dir()]
        except OSError:
            continue
        for child in children:
            if child.name == HDD_CACHE_DIR_NAME:
                continue
            if child.name.lower() in IGNORED_COMMON_DIRS:
                continue
            if os.path.normcase(os.path.abspath(str(child))) in referenced_dirs:
                continue
            report.anomalies.append(
                Anomaly(
                    kind="unmanaged_folder",
                    level="warning",
                    message=(
                        f"{child} 没有对应的 Steam 清单（可能是搬移或卸载残留），"
                        "程序不会自动删除它"
                    ),
                    path=str(child),
                )
            )

    # ---- .hdd_cache 孤儿备份扫描 ----
    for library in libraries:
        backup_root = library.hdd_cache
        if not backup_root.is_dir():
            continue
        try:
            children = [p for p in backup_root.iterdir() if p.is_dir()]
        except OSError:
            continue
        for child in children:
            if os.path.normcase(str(child)) in referenced_backups:
                continue
            report.anomalies.append(
                Anomaly(
                    kind="orphan_backup",
                    level="error",
                    message=f"发现没有对应清单的母本备份：{child}（可能是上次操作中断，请用修复向导处理）",
                    path=str(child),
                )
            )

    # ---- 隔离区：等待用户确认删除的多余文件（回写时搬进去的） ----
    if config.mother_drive:
        root = trash_root(config.mother_drive)
        if root.is_dir():
            try:
                items = quarantine_items(config.mother_drive)
            except Exception as exc:  # noqa: BLE001 - 隔离区读取异常不应让扫描失败
                log.warning("读取隔离区失败：%s", exc)
                items = []
            report.quarantine_items = list(items)
            for item in items:
                report.anomalies.append(
                    Anomaly(
                        kind="quarantine",
                        level="warning",
                        message=(
                            f"隔离区有 {item.files} 个文件 / {item.size_text} 等待你确认"
                            f"（游戏能正常运行后再删除）：{item.item_dir}"
                        ),
                        path=item.item_dir,
                        appid=item.appid,
                    )
                )

    report.games.sort(key=lambda g: (g.library_path.lower(), g.name.lower()))
    return report


def _classify_game(game: GameRecord, cfg: Config, in_progress: dict[str, Any]) -> None:
    """结合文件系统事实判定游戏当前状态。"""
    game_path = Path(game.game_path) if game.game_path else None
    backup = Path(game.hdd_backup_path) if game.hdd_backup_path else None
    cache_copy = Path(game.cache_copy_path) if game.cache_copy_path else None

    game.hdd_backup_exists = bool(backup and backup.is_dir())
    game.cache_copy_exists = bool(cache_copy and cache_copy.is_dir())

    task = in_progress.get(game.appid) if isinstance(in_progress, dict) else None
    task_phase = (task or {}).get("phase") if isinstance(task, dict) else None

    # 状态 1：原位置是 Junction → 已加速（必须验证目标在缓存根之下）
    if game_path is not None and is_junction(game_path):
        game.junction_target = junction_target(game_path)
        game.status = ST_ACCELERATED
        expected = os.path.normcase(os.path.abspath(str(cache_copy))) if cache_copy else ""
        actual = os.path.normcase(os.path.abspath(game.junction_target)) if game.junction_target else ""
        if not game.junction_target:
            game.notes.append("联接目标读取失败")
        elif expected and actual != expected:
            game.status = ST_UNKNOWN
            game.notes.append(f"联接目标与记录不符：{game.junction_target}")
        elif not game.cache_copy_exists:
            game.status = ST_UNKNOWN
            game.notes.append("联接指向的 SSD 副本不存在，需要修复")
        if task_phase == "writing_back":
            game.status = ST_WRITING_BACK
        return

    # 状态 2：原位置是真实目录 → 按所选的两个盘区分
    if game_path is not None and game_path.is_dir():
        role = cfg.role_of_drive(drive_of(game_path))
        if role == "cache":
            game.status = ST_ON_SSD
        elif role == "mother":
            game.status = ST_ON_HDD
        else:
            game.status = ST_ON_OTHER
            game.notes.append(
                f"盘符 {drive_of(game_path)}: 不是所选的母盘/加速盘，本工具不处理它"
            )
        return

    # 状态 3：原位置不存在，但 .hdd_cache 有母本 → 加速中断（或回写中断）
    if game.hdd_backup_exists:
        game.status = ST_WRITING_BACK if task_phase == "writing_back" else ST_ACCELERATING
        game.notes.append("检测到未完成的加速/回写，请用修复向导处理")
        return

    # 状态 4：原位置与备份都不存在 → ACF 残留
    game.status = ST_MISSING
    game.notes.append("清单存在但游戏目录不存在（Steam 中可能显示为未安装）")


def scan_summary(report: ScanReport) -> str:
    """把扫描结果汇总成一段人类可读文本（CLI 与自检向导都会用到）。"""
    lines: list[str] = []
    lines.append(f"Steam 根目录：{report.steam_root or '(未找到)'}")
    if report.cache_dir_configured:
        lines.append(f"缓存目录：{report.cache_dir}")
    else:
        lines.append(f"缓存目录：尚未选择（建议值 {report.cache_dir}，请在设置页按盘符角色确认）")
    lines.append(f"游戏总数：{len(report.games)}；异常 {len(report.anomalies)} 条")
    counts = {status: len(items) for status, items in report.games_by_status().items()}
    for status, label in STATUS_LABELS.items():
        if counts.get(status):
            lines.append(f"  {label}: {counts[status]}")
    return "\n".join(lines)


__all__ = [
    "Anomaly",
    "GameRecord",
    "ScanReport",
    "SteamLibrary",
    "STATUS_LABELS",
    "ST_ACCELERATED",
    "ST_ACCELERATING",
    "ST_MISSING",
    "ST_ON_HDD",
    "ST_ON_SSD",
    "ST_ON_OTHER",
    "ST_UNKNOWN",
    "ST_WRITING_BACK",
    "discover_libraries",
    "find_steam_root",
    "human_size",
    "is_junction",
    "junction_target",
    "parse_vdf",
    "read_acf",
    "scan",
    "scan_summary",
]
