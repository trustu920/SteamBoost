"""隔离区：镜像回写时"母盘多出来的文件"先搬到这里，等用户确认游戏能跑再删。

为什么这么做（用户明确要求的方案）
----------------------------------
直接删除母盘上的多余文件是**不可逆**的。改成：

1. 回写前把母盘多出来的文件**移动**到 ``<母盘>:\\SteamBoostTrash\\<appid>_<名称>_<时间戳>\\``；
2. 然后执行镜像复制 —— 此时母盘已经不多任何文件，镜像天然一致，**全程没有删除动作**；
3. 用户启动游戏确认正常后，再点"删除多余文件"才真正释放空间（也可以"还原"搬回去）。

设计约束
--------
* 隔离区必须与母本**同卷**：这样"移动"是改名操作，瞬间完成且不复制数据；
* 移动前校验：源必须在母本游戏目录之内，目标必须在隔离区根之内；
* 每个隔离项都写一份清单文件 ``_steamboot_quarantine.json``；
* 删除隔离项时要求"位于隔离区根之内 + 自己是隔离项（有清单）"，
  两道校验都过才允许递归删除——这是全程序唯一允许递归删除的地方。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from config import TRASH_DIR_NAME, Config, drive_of, normalize_drive, trash_root
from deletion_guard import (
    KIND_QUARANTINE,
    build_request,
    confirm_deletion,
    safe_remove_tree,
)
from junction_utils import PathSafetyError, assert_within, is_within
from logger import setup_logger

log = setup_logger("steamboot.quarantine")

#: 隔离项清单文件名（也是"这确实是隔离项"的凭据）
MANIFEST_NAME = "_steamboot_quarantine.json"


class QuarantineError(RuntimeError):
    """隔离区操作失败。"""


@dataclass
class QuarantineItem:
    """一个隔离项（对应隔离区根下的一个目录）。"""

    item_dir: str
    appid: str = ""
    name: str = ""
    mother_path: str = ""
    created: float = 0.0
    files: int = 0
    bytes: int = 0
    entries: list[str] = field(default_factory=list)

    @property
    def size_text(self) -> str:
        from config import human_size

        return human_size(self.bytes)

    @property
    def created_text(self) -> str:
        if not self.created:
            return ""
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.created))


# ------------------------------------------------------------------ 移入隔离区
def quarantine_extras(
    extras: list[str],
    mother_dir: str | os.PathLike[str],
    *,
    appid: str,
    game_name: str,
    mother_drive: str,
    timestamp: float | None = None,
) -> QuarantineItem:
    """把母本目录里"多余的文件"移动到隔离区。

    ``extras`` 是相对母本目录的路径列表（来自 ``copy_engine.plan_extras``）。
    返回隔离项记录；调用方应把它写进索引文件供界面展示。
    """
    mother = Path(os.path.abspath(str(mother_dir)))
    if not mother.is_dir():
        raise QuarantineError(f"母本目录不存在：{mother}")

    created = timestamp or time.time()
    safe_name = _safe_component(game_name) or appid
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(created))
    root = trash_root(mother_drive)
    item_dir = root / f"{appid}_{safe_name}_{stamp}"

    # 隔离区必须与母本同卷，否则"移动"会变成跨盘复制（慢且可能中途失败）
    if normalize_drive(drive_of(mother)) != normalize_drive(mother_drive):
        raise QuarantineError(
            f"母本目录不在所选母盘上：{mother}（母盘 {mother_drive}:）"
        )

    item = QuarantineItem(
        item_dir=str(item_dir),
        appid=appid,
        name=game_name,
        mother_path=str(mother),
        created=created,
    )
    if not extras:
        return item

    try:
        item_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise QuarantineError(f"隔离项目录已存在（同一秒内重复操作？）：{item_dir}") from exc

    moved: list[str] = []
    failed: list[tuple[str, str]] = []
    # 先搬文件，再搬（此时已空的）目录，顺序由 plan_extras 保证
    for rel in extras:
        source = mother / rel
        target = item_dir / rel
        try:
            if not source.exists() and not os.path.lexists(str(source)):
                continue
            assert_within(source, mother, "待隔离的文件")
            assert_within(target, item_dir, "隔离区目标")
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir() and not source.is_symlink():
                # 目录：此刻正常情况下已经空了（里面的文件已被逐个搬走）。
                # 若隔离区里已经有同名目录（搬文件时创建过），就只删掉这个空壳，
                # 绝不用 shutil.move 往已存在的目录里塞——那会多嵌套一层。
                if target.exists():
                    try:
                        source.rmdir()  # 只删空目录；非空会抛 OSError
                    except OSError as exc:
                        failed.append((rel, f"目录非空，未隔离：{exc}"))
                    moved.append(rel)
                else:
                    shutil.move(str(source), str(target))
                    moved.append(rel)
            else:
                stat_result = source.stat()
                os.replace(source, target)  # 同卷改名，瞬间完成
                item.files += 1
                item.bytes += stat_result.st_size
            moved.append(rel)
        except (OSError, PathSafetyError, shutil.Error) as exc:
            failed.append((rel, str(exc)))
            log.error("隔离失败：%s → %s：%s", source, target, exc)

    item.entries = moved
    _write_manifest(item)

    if failed:
        detail = "；".join(f"{rel}（{reason}）" for rel, reason in failed[:5])
        raise QuarantineError(
            f"有 {len(failed)} 个文件无法移入隔离区，镜像回写已中止：{detail}"
        )
    log.info(
        "已隔离 %d 个文件 / %d 字节 → %s", item.files, item.bytes, item_dir
    )
    return item


def _write_manifest(item: QuarantineItem) -> None:
    """写入隔离项清单（删除时的凭据）。"""
    path = Path(item.item_dir) / MANIFEST_NAME
    payload = asdict(item)
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise QuarantineError(f"无法写入隔离清单：{path}：{exc}") from exc


# ------------------------------------------------------------------ 读取/管理
def read_item(item_dir: str | os.PathLike[str]) -> QuarantineItem | None:
    """读取隔离项；没有清单文件就返回 None（说明它不是隔离项）。"""
    directory = Path(item_dir)
    manifest = directory / MANIFEST_NAME
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    known = {f for f in QuarantineItem.__dataclass_fields__}  # type: ignore[attr-defined]
    return QuarantineItem(**{k: v for k, v in data.items() if k in known})


def list_items(mother_drive: str) -> list[QuarantineItem]:
    """列出某个母盘隔离区里的所有隔离项。"""
    root = trash_root(mother_drive)
    if not root.is_dir():
        return []
    items: list[QuarantineItem] = []
    try:
        children = [path for path in root.iterdir() if path.is_dir()]
    except OSError:
        return []
    for child in children:
        item = read_item(child)
        if item is not None:
            items.append(item)
    items.sort(key=lambda entry: entry.created, reverse=True)
    return items


def list_all(mother_drives: list[str] | None = None) -> list[QuarantineItem]:
    """列出若干母盘上的隔离项（界面"隔离区"页用）。"""
    drives = mother_drives or [Config.load().mother_drive]
    found: list[QuarantineItem] = []
    for drive in drives:
        if normalize_drive(drive):
            found.extend(list_items(drive))
    return found


def purge_item(item_dir: str | os.PathLike[str], mother_drive: str, confirmer=None) -> int:
    """**真正删除**一个隔离项，返回释放的字节数。

    三道前置校验（不通过连问都不问）：

    1. 目标必须是隔离区根的直接子目录，且不等于隔离区根；
    2. 目标必须带隔离清单（证明它是本程序创建的隔离项）；
    3. 目标路径不得位于任何 Steam 库内。

    然后交给 :mod:`deletion_guard` 请求**人类确认**——没有确认者时一律拒绝执行，
    因此无人值守运行不可能删掉任何东西。
    """
    root = trash_root(mother_drive)
    directory = Path(os.path.abspath(str(item_dir)))

    assert_within(directory, root, "待删除的隔离项")
    if os.path.normcase(str(directory)) == os.path.normcase(str(root)):
        raise PathSafetyError(f"拒绝删除隔离区根目录本身：{root}")
    if os.path.normcase(os.path.dirname(str(directory))) != os.path.normcase(str(root)):
        raise PathSafetyError(f"拒绝删除隔离区根的直接子目录以外的对象：{directory}")

    item = read_item(directory)
    if item is None:
        raise PathSafetyError(
            f"拒绝删除：该目录不是本程序创建的隔离项（缺少 {MANIFEST_NAME}）：{directory}"
        )

    request = build_request(
        KIND_QUARANTINE,
        directory,
        root,
        reason=f"删除隔离项：{item.appid} {item.name}（用户确认游戏可正常运行后）",
        reversible=False,
    )
    token = confirm_deletion(request, confirmer)
    freed = safe_remove_tree(token)
    log.info("已删除隔离项 %s（释放 %d 字节）", directory, freed)
    return freed


def restore_item(item_dir: str | os.PathLike[str], mother_drive: str) -> int:
    """把隔离项里的文件**还原回母本目录**，返回还原的文件数。

    用于"用户发现游戏有问题，想把多余文件放回去"的场景。
    """
    root = trash_root(mother_drive)
    directory = Path(os.path.abspath(str(item_dir)))
    assert_within(directory, root, "待还原的隔离项")
    item = read_item(directory)
    if item is None:
        raise PathSafetyError(f"不是隔离项，无法还原：{directory}")

    mother = Path(item.mother_path)
    restored = 0
    for path in sorted(directory.rglob("*"), key=lambda p: len(str(p)), reverse=True):
        if path.name == MANIFEST_NAME:
            continue
        target = mother / path.relative_to(directory)
        try:
            if path.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, target) if not target.exists() else shutil.copy2(path, target)
                restored += 1
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
        except OSError as exc:
            log.error("还原失败：%s → %s：%s", path, target, exc)
    try:
        (directory / MANIFEST_NAME).unlink()
        directory.rmdir()
    except OSError:
        pass
    log.info("已从隔离区还原 %d 个文件到 %s", restored, mother)
    return restored


def _safe_component(text: str) -> str:
    """把游戏名清洗成安全的目录名片段。"""
    cleaned = "".join(ch for ch in (text or "") if ch not in '\\/:*?"<>|').strip()
    return cleaned[:40]
