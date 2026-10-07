"""SteamBoost 配置管理。

配置文件：%LOCALAPPDATA%\\SteamBoost\\config.json

设计原则（按用户确认的模型）：
- 设置页只让用户选**两个盘**：``母盘``（存游戏母本）与 ``加速盘``（存 SSD 缓存副本），
  程序不猜测磁盘介质类型；
- 程序只校验这两个盘上的 Steam 库：两盘不能相同、缓存目录不能落在任一库内、
  母盘上必须至少有一个 Steam 库；
- 任何字段缺失都能补默认值；配置损坏时回退默认，绝不因此崩溃。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "SteamBoost"
CONFIG_FILENAME = "config.json"

#: 缓存副本目录名前缀，完整形如 ``<appid>_<installdir>``
CACHE_DIR_NAME = "SteamBoostCache"
#: 母本暂存目录名，位于每个 Steam 库的 ``steamapps\\common`` 之下
HDD_CACHE_DIR_NAME = ".hdd_cache"
#: 隔离区目录名（位于母盘根下）：镜像回写时"母盘多出来的文件"先搬到这里
TRASH_DIR_NAME = "SteamBoostTrash"

#: 校验强度档位
VERIFY_METADATA = "metadata"   # 文件数 + 总字节 + 逐文件大小/时间戳（已选档位）
VERIFY_MANIFEST = "manifest"   # 追加逐文件哈希清单
VERIFY_NONE = "none"

#: 复制引擎
ENGINE_FASTCOPY = "fastcopy"   # 默认：FastCopy（差异复制 / 镜像回写）
ENGINE_ROBOCOPY = "robocopy"   # 回退：Windows 自带


def app_data_dir() -> Path:
    """返回应用数据根目录（配置、状态、封面缓存、日志都放这里）。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ("." + APP_NAME.lower())


def normalize_drive(value: str) -> str:
    """把各种写法的盘符统一成大写单字母，例如 ``d:`` / ``D:`` / ``d`` / ``D`` → ``D``。"""
    text = (value or "").strip().rstrip(":\\/")
    return text[:1].upper() if text else ""


def drive_of(path: str | os.PathLike[str]) -> str:
    """取出路径所在的盘符（大写单字母），异常路径返回空串。"""
    text = os.path.abspath(str(path))
    return normalize_drive(os.path.splitdrive(text)[0])


def volume_free_bytes(drive: str) -> int:
    """返回某盘符的剩余字节数；盘不存在时返回 -1。"""
    letter = normalize_drive(drive)
    if not letter:
        return -1
    try:
        return shutil.disk_usage(f"{letter}:\\").free
    except OSError:
        return -1


def volume_total_bytes(drive: str) -> int:
    """返回某盘符的总容量字节数；盘不存在时返回 -1。"""
    letter = normalize_drive(drive)
    if not letter:
        return -1
    try:
        return shutil.disk_usage(f"{letter}:\\").total
    except OSError:
        return -1


def list_volumes() -> list[dict[str, Any]]:
    """列出当前所有可用盘符（设置页的下拉框直接用这份数据）。

    刻意不使用 WMI 判断介质类型——介质角色完全由用户指定。
    """
    volumes: list[dict[str, Any]] = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        volumes.append(
            {
                "drive": letter,
                "root": root,
                "total": usage.total,
                "free": usage.free,
                "used": usage.used,
            }
        )
    return volumes


def default_cache_dir(cache_drive: str) -> str:
    """给定加速盘，返回默认缓存根目录：``<加速盘>:\\SteamBoostCache``。"""
    letter = normalize_drive(cache_drive) or normalize_drive(os.environ.get("SystemDrive", "C"))
    return str(Path(f"{letter}:\\") / CACHE_DIR_NAME)


def trash_root(mother_drive: str) -> Path:
    """隔离区根目录：``<母盘>:\\SteamBoostTrash``（必须与母本同卷，移动才是瞬时的）。"""
    letter = normalize_drive(mother_drive)
    return Path(f"{letter}:\\") / TRASH_DIR_NAME


def human_size(num_bytes: int | float | None) -> str:
    """字节数转人类可读文本。"""
    value = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TB"


@dataclass
class Config:
    """应用配置。字段名即 JSON 键名。"""

    #: 母盘：存放游戏母本的盘符（用户选择）
    mother_drive: str = ""
    #: 加速盘：存放 SSD 缓存副本的盘符（用户选择）
    cache_drive: str = ""
    #: 加速盘上的缓存根目录；空串表示尚未选择
    cache_dir: str = ""
    #: 复制引擎：fastcopy（默认）/ robocopy（回退）
    copy_engine: str = ENGINE_FASTCOPY
    #: FastCopy 可执行文件路径；空串表示自动查找
    fastcopy_path: str = ""
    #: robocopy 线程数（仅在回退到 robocopy 时生效）
    robocopy_threads: int = 8
    #: 校验强度档位
    verification: str = VERIFY_METADATA
    #: 镜像回写时是否启用 FastCopy 的 xxHash3 校验（档位 ① 之外的额外保险）
    fastcopy_verify: bool = False
    #: SSD 空间提醒阈值（满足任一即提醒）
    free_space_min_percent: float = 10.0
    free_space_min_gb: float = 20.0
    #: 日志目录；空串表示 <app_data>/logs
    log_dir: str = ""
    #: 封面缓存目录；空串表示 <app_data>/covers
    cover_cache_dir: str = ""
    #: 关闭窗口时最小化到托盘
    minimize_to_tray: bool = True

    # ------------------------------------------------------------------ 路径
    def resolved_cache_dir(self) -> Path:
        """有效缓存根目录（未配置时给出建议值，仅用于展示，不落盘）。"""
        if self.cache_dir.strip():
            return Path(self.cache_dir)
        return Path(default_cache_dir(self.cache_drive))

    def resolved_trash_root(self) -> Path:
        """隔离区根目录。"""
        return trash_root(self.mother_drive)

    def resolved_log_dir(self) -> Path:
        return Path(self.log_dir) if self.log_dir.strip() else app_data_dir() / "logs"

    def resolved_cover_dir(self) -> Path:
        return Path(self.cover_cache_dir) if self.cover_cache_dir.strip() else app_data_dir() / "covers"

    def state_file(self) -> Path:
        """加速/释放过程中的状态文件（异常恢复要用）。"""
        return app_data_dir() / "state.json"

    def quarantine_file(self) -> Path:
        """隔离区索引文件（列出待用户确认删除的项）。"""
        return app_data_dir() / "quarantine.json"

    # ------------------------------------------------------------------ 角色
    def role_of_drive(self, drive: str) -> str | None:
        """返回盘符角色：``mother``（母盘）/ ``cache``（加速盘）/ ``None``（未选中）。"""
        letter = normalize_drive(drive)
        if not letter:
            return None
        if letter == normalize_drive(self.cache_drive):
            return "cache"
        if letter == normalize_drive(self.mother_drive):
            return "mother"
        return None

    # -------------------------------------------------------------- 目录校验
    def validate(self, libraries: list[str] | list[tuple[str, str]]) -> tuple[list[str], list[str]]:
        """校验两个盘与缓存目录，返回 ``(致命问题, 提醒)``。

        ``libraries`` 可以是库路径列表，或 ``(库路径, 盘符)`` 列表；
        程序**只关心这两个盘上的 Steam 库**，其他盘与本工具无关。
        """
        problems: list[str] = []
        warnings: list[str] = []

        mother = normalize_drive(self.mother_drive)
        cache_drive = normalize_drive(self.cache_drive)

        if not mother:
            problems.append("尚未选择母盘（存放游戏母本的盘）")
        if not cache_drive:
            problems.append("尚未选择加速盘（存放 SSD 缓存副本的盘）")
        if mother and cache_drive and mother == cache_drive:
            problems.append(f"母盘与加速盘不能是同一个盘（{mother}:），否则加速没有意义")

        pairs: list[tuple[str, str]] = []
        for item in libraries or []:
            if isinstance(item, tuple):
                pairs.append((str(item[0]), normalize_drive(item[1])))
            else:
                text = str(item)
                pairs.append((text, drive_of(text)))

        if mother and not any(drive == mother for _, drive in pairs):
            problems.append(f"母盘 {mother}: 上没有找到任何 Steam 库")

        raw_cache = self.cache_dir.strip()
        if not raw_cache:
            problems.append("尚未选择缓存目录")
            return problems, warnings

        cache_path = Path(os.path.abspath(raw_cache))
        cache_letter = drive_of(cache_path)
        if not cache_letter:
            problems.append(f"缓存目录必须是带盘符的本地绝对路径：{raw_cache}")
        elif cache_drive and cache_letter != cache_drive:
            problems.append(f"缓存目录位于 {cache_letter}:，与所选加速盘 {cache_drive}: 不一致")

        # 只校验这两个盘上的库：缓存目录绝不能落在任何 Steam 库内部
        for lib_path, lib_drive in pairs:
            if lib_drive not in {mother, cache_drive}:
                continue
            if _is_same_or_inside(cache_path, Path(lib_path)):
                problems.append(f"缓存目录不能位于 Steam 库内部：{lib_path}")

        if cache_letter and cache_letter == mother and mother:
            warnings.append(f"缓存目录位于母盘 {mother}: 上，加速将失去意义")

        for lib_path, lib_drive in pairs:
            if lib_drive == cache_drive:
                warnings.append(f"加速盘 {cache_drive}: 上存在 Steam 库：{lib_path}（该库中的游戏本身已在 SSD）")

        return problems, warnings

    # ------------------------------------------------------------ 读写持久化
    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """读取配置；文件缺失或损坏时返回默认配置。"""
        cfg_path = path or (app_data_dir() / CONFIG_FILENAME)
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("配置根节点不是对象")
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError, json.JSONDecodeError):
            return cls()

        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {key: value for key, value in data.items() if key in known}
        for key in ("mother_drive", "cache_drive"):
            if key in clean:
                clean[key] = normalize_drive(str(clean[key]))
        try:
            return cls(**clean)
        except TypeError:
            return cls()

    def save(self, path: Path | None = None) -> Path:
        """原子写回配置（先写临时文件再替换，避免半截文件）。"""
        cfg_path = path or (app_data_dir() / CONFIG_FILENAME)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, cfg_path)
        return cfg_path

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_same_or_inside(child: Path, parent: Path) -> bool:
    """判断 child 是否等于 parent 或位于 parent 之内（大小写不敏感）。"""
    try:
        c = os.path.normcase(os.path.abspath(str(child)))
        p = os.path.normcase(os.path.abspath(str(parent)))
    except (OSError, ValueError):
        return False
    if c == p:
        return True
    return c.startswith(p.rstrip("\\/") + os.sep)
