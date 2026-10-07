"""日志模块：调试日志 + 操作审计日志（都按天滚动）。

- 调试日志 ``steamboot.log``：程序运行细节，便于排查。
- 审计日志 ``operations.log``：每次加速/释放的源路径、目标路径、字节数、结果。
  需求要求「全程写操作日志，记录每次加速/释放的路径、大小、结果」，
  因此审计日志单独成文件，不与调试日志混在一起。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from config import Config

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_OP_FORMAT = "%(asctime)s | %(message)s"
_configured: dict[str, logging.Logger] = {}


def _log_dir(cfg: Config | None = None) -> Path:
    """返回当前生效的日志目录。

    优先级：环境变量 ``STEAMBOOST_LOG_DIR``（测试 / 便携模式）→ 配置里的 ``log_dir``
    → ``<app_data>/logs``。测试会把它指向沙箱，避免污染真实审计日志。
    """
    override = os.environ.get("STEAMBOOST_LOG_DIR", "").strip()
    directory = Path(override) if override else (cfg or Config.load()).resolved_log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def current_log_dir(cfg: Config | None = None) -> Path:
    """当前日志目录（界面显示与测试断言用，避免各处自己拼路径）。"""
    return _log_dir(cfg)


def log_usage(cfg: Config | None = None) -> tuple[int, int]:
    """返回日志目录的 ``(文件数, 总字节)``，供设置页显示占用。"""
    directory = _log_dir(cfg)
    count = 0
    total = 0
    try:
        for path in directory.glob("*.log*"):
            if path.is_file():
                count += 1
                total += path.stat().st_size
    except OSError:
        pass
    return count, total


def prune_logs(keep_days: int = 30, confirmer=None, cfg: Config | None = None) -> int:
    """删除超过 ``keep_days`` 天的日志文件，返回释放字节数。

    **必须由用户确认**：内部走 deletion_guard 的删除闸门（``KIND_LOG_FILE``），
    没有确认者时一个文件也删不掉。

    说明：按天滚动的处理器本身也会保留固定天数并自动清掉更早的备份
    （见 :func:`_daily_handler` 的 ``backupCount``）——那是对程序自己生成文件的
    常规维护；本函数提供的是用户主动触发的清理。
    """
    import time as _time

    from deletion_guard import KIND_LOG_FILE, delete  # 延迟导入，避免循环依赖

    directory = _log_dir(cfg)
    cutoff = _time.time() - max(1, int(keep_days)) * 86400
    freed = 0
    logger = setup_logger("steamboot", cfg)
    try:
        candidates = sorted(directory.glob("*.log*"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if not path.is_file() or path.stat().st_mtime >= cutoff:
                continue
            freed += delete(
                KIND_LOG_FILE,
                path,
                directory,
                confirmer=confirmer,
                reason=f"清理 {keep_days} 天前的日志",
            )
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不应中断整体清理
            logger.warning("清理日志失败：%s（%s）", path, exc)
    return freed


class _SafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """按天滚动、且**日志目录被外部删除时能自动重建**的处理器。

    为什么需要：目录可能被清理（测试收尾删沙箱、用户手动删日志目录、
    日志放在被拔掉的移动盘上）。原版处理器此时会在写入时抛异常并往 stderr
    打一堆堆栈，看起来像程序出错；这里改成自动重建目录，实在不行才降级报错。
    """

    def _ensure_dir(self) -> None:
        directory = Path(self.baseFilename).parent
        if not directory.is_dir():
            directory.mkdir(parents=True, exist_ok=True)

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            self._ensure_dir()
            super().emit(record)
        except Exception:  # noqa: BLE001 - 日志失败绝不能影响主流程
            self.handleError(record)


def _daily_handler(path: Path, backup_days: int, fmt: str) -> TimedRotatingFileHandler:
    """按天滚动的文件处理器；encoding 固定 utf-8，避免中文游戏名乱码。"""
    handler = _SafeTimedRotatingFileHandler(
        filename=str(path),
        when="midnight",
        interval=1,
        backupCount=backup_days,
        encoding="utf-8",
        delay=True,
    )
    handler.suffix = "%Y-%m-%d"
    handler.setFormatter(logging.Formatter(fmt))
    return handler


def setup_logger(name: str = "steamboot", cfg: Config | None = None, level: int = logging.INFO) -> logging.Logger:
    """初始化并返回调试日志器（重复调用返回已配置实例）。"""
    key = f"debug:{name}"
    if key in _configured:
        return _configured[key]

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    try:
        logger.addHandler(_daily_handler(_log_dir(cfg) / "steamboot.log", 14, _LOG_FORMAT))
    except OSError as exc:  # 日志目录不可写也不能让程序起不来
        print(f"[warn] 无法创建日志文件：{exc}", file=sys.stderr)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(stream)
    _configured[key] = logger
    return logger


def get_operation_logger(cfg: Config | None = None) -> logging.Logger:
    """返回操作审计日志器（每次加速/释放必须写一条）。"""
    key = "operation"
    if key in _configured:
        return _configured[key]

    logger = logging.getLogger("steamboot.operations")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    try:
        logger.addHandler(_daily_handler(_log_dir(cfg) / "operations.log", 90, _OP_FORMAT))
    except OSError as exc:
        print(f"[warn] 无法创建操作日志：{exc}", file=sys.stderr)
    _configured[key] = logger
    return logger


def log_operation(
    action: str,
    *,
    appid: str | int | None = None,
    name: str = "",
    src: str = "",
    dst: str = "",
    size_bytes: int | None = None,
    result: str = "",
    detail: str = "",
    cfg: Config | None = None,
) -> None:
    """写一条操作审计记录：加速/释放/回滚/删除 的路径、大小与结果。

    action 建议取值：``accelerate`` / ``release`` / ``rollback`` / ``junction_create`` /
    ``junction_remove`` / ``cache_erase`` / ``preflight``。
    """
    size_text = f"{size_bytes}" if size_bytes is not None else "-"
    parts = [
        f"action={action}",
        f"appid={appid if appid is not None else '-'}",
        f"name={name or '-'}",
        f"src={src or '-'}",
        f"dst={dst or '-'}",
        f"bytes={size_text}",
        f"result={result or '-'}",
    ]
    if detail:
        parts.append(f"detail={detail}")
    get_operation_logger(cfg).info(" ".join(parts))


def logs_present(cfg: Config | None = None) -> list[str]:
    """列出当前日志目录下的文件（供设置页/诊断显示）。"""
    directory = _log_dir(cfg)
    try:
        return sorted(str(p) for p in directory.glob("*.log*"))
    except OSError:
        return []


def close_loggers() -> None:
    """关闭并摘除本模块创建的所有日志处理器。

    用处：程序退出前、或需要删除日志文件/目录时（Windows 不允许删除被打开的文件）。
    调用后再写日志会重新创建处理器，因此应在"确实不再记录日志"之后调用。
    """
    for key in list(_configured):
        logger = _configured.pop(key)
        for handler in list(logger.handlers):
            try:
                handler.flush()
                handler.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应抛给调用方
                pass
            logger.removeHandler(handler)


__all__ = [
    "close_loggers",
    "current_log_dir",
    "get_operation_logger",
    "log_operation",
    "log_usage",
    "logs_present",
    "prune_logs",
    "setup_logger",
]
