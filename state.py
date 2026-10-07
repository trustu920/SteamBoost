"""任务状态持久化（state.json）。

作用
----
1. 记录"当前哪些游戏处于加速中/加速态/回写中"，供扫描器判定状态；
2. 记录每个游戏的母本备份路径、缓存副本路径、联接路径，修复向导据此恢复；
3. 记录最近一次回写时间与对应的隔离项，界面据此提示"待确认删除"。

崩溃安全：写入用"临时文件 + 原子替换"，任何时刻磁盘上要么是旧内容要么是新内容，
不会出现半截 JSON。文件损坏时回退为空状态（而不是让程序起不来）。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from config import Config
from logger import setup_logger

log = setup_logger("steamboot.state")

STATE_VERSION = 1

#: 阶段取值（扫描器按这些字符串判定"加速中断/回写中断"）
PHASE_ACCELERATING = "accelerating"
PHASE_ACCELERATED = "accelerated"
PHASE_WRITING_BACK = "writing_back"
PHASE_RELEASING = "releasing"
PHASE_FAILED = "failed"

PHASE_LABELS = {
    PHASE_ACCELERATING: "加速中",
    PHASE_ACCELERATED: "已加速",
    PHASE_WRITING_BACK: "回写中",
    PHASE_RELEASING: "释放中",
    PHASE_FAILED: "失败待修复",
}


@dataclass
class TaskState:
    """某个游戏的一次加速/回写/释放任务的持久化状态。"""

    appid: str
    name: str = ""
    installdir: str = ""
    library: str = ""
    #: 母本备份路径（<库>\steamapps\common\.hdd_cache\<installdir>）
    mother_path: str = ""
    #: SSD 缓存副本路径（<缓存根>\<appid>_<installdir>）
    cache_copy: str = ""
    #: 原位置（加速后是目录联接）
    junction: str = ""
    phase: str = ""
    started: float = 0.0
    updated: float = 0.0
    last_writeback: float = 0.0
    #: 最近一次回写产生的隔离项目录（等待用户确认删除）
    quarantine_item: str = ""
    notes: str = ""

    @property
    def phase_label(self) -> str:
        return PHASE_LABELS.get(self.phase, self.phase or "未知")

    def touch(self) -> None:
        self.updated = time.time()


class StateStore:
    """state.json 的读写封装。"""

    def __init__(self, path: Path | None = None, cfg: Config | None = None) -> None:
        self.path = Path(path) if path else (cfg or Config.load()).state_file()
        self.tasks: dict[str, TaskState] = {}
        self.load()

    # ------------------------------------------------------------ 读写
    def load(self) -> None:
        self.tasks = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.warning("状态文件损坏，按空状态处理：%s（%s）", self.path, exc)
            return
        if not isinstance(data, dict):
            return
        raw_tasks = data.get("tasks")
        if not isinstance(raw_tasks, dict):
            return
        known = {f for f in TaskState.__dataclass_fields__}  # type: ignore[attr-defined]
        for appid, payload in raw_tasks.items():
            if not isinstance(payload, dict):
                continue
            clean = {k: v for k, v in payload.items() if k in known}
            clean["appid"] = str(clean.get("appid") or appid)
            try:
                self.tasks[str(appid)] = TaskState(**clean)
            except TypeError:
                continue

    def save(self) -> Path:
        """原子写入（先写临时文件再替换）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "updated": time.time(),
            "tasks": {appid: asdict(task) for appid, task in self.tasks.items()},
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        return self.path

    # ------------------------------------------------------------ 查询
    def get(self, appid: str | int) -> TaskState | None:
        return self.tasks.get(str(appid))

    def phase_of(self, appid: str | int) -> str:
        task = self.get(appid)
        return task.phase if task else ""

    def accelerated_tasks(self) -> list[TaskState]:
        return [t for t in self.tasks.values() if t.phase in (PHASE_ACCELERATED, PHASE_WRITING_BACK)]

    # ------------------------------------------------------------ 写入
    def upsert(self, task: TaskState) -> TaskState:
        task.touch()
        self.tasks[task.appid] = task
        self.save()
        return task

    def set_phase(self, appid: str | int, phase: str, **changes: Any) -> TaskState | None:
        task = self.get(appid)
        if task is None:
            return None
        task.phase = phase
        for key, value in changes.items():
            if hasattr(task, key):
                setattr(task, key, value)
        return self.upsert(task)

    def remove(self, appid: str | int) -> None:
        if str(appid) in self.tasks:
            del self.tasks[str(appid)]
            self.save()

    def to_dict(self) -> dict[str, Any]:
        """返回扫描器需要的结构：``{"tasks": {appid: {...}}}``。"""
        return {"version": STATE_VERSION, "tasks": {appid: asdict(task) for appid, task in self.tasks.items()}}


def build_task(game, cfg: Config) -> TaskState:
    """按扫描到的游戏记录与配置，构造一个任务状态对象。"""
    from steam_scanner import HDD_CACHE_DIR_NAME  # 延迟导入，避免循环

    library = Path(game.library_path)
    installdir = game.installdir
    return TaskState(
        appid=str(game.appid),
        name=game.name,
        installdir=installdir,
        library=str(library),
        mother_path=str(library / "steamapps" / "common" / HDD_CACHE_DIR_NAME / installdir),
        cache_copy=str(Path(cfg.resolved_cache_dir()) / f"{game.appid}_{installdir}"),
        junction=str(game.game_path),
        started=time.time(),
    )


__all__ = [
    "PHASE_ACCELERATED",
    "PHASE_ACCELERATING",
    "PHASE_FAILED",
    "PHASE_LABELS",
    "PHASE_RELEASING",
    "PHASE_WRITING_BACK",
    "STATE_VERSION",
    "StateStore",
    "TaskState",
    "build_task",
]
