"""封面图：异步下载 + 本地缓存 + 占位图。

要点
----
* **绝不阻塞 UI**：命中缓存立刻返回，未命中丢进 ``QThreadPool`` 后台下载，
  下载完成通过信号回到主线程，界面再刷新那一张卡片。
* 缓存文件名固定为 ``<appid>.jpg``，放在 ``<app_data>/covers/``；
  失败（无网络 / 该游戏没有竖版封面）时不反复重试，落一个 ``.fail`` 标记。
* 占位图用 QPainter 现画（渐变 + 游戏名），不依赖任何外部资源。
"""

from __future__ import annotations

import urllib.error
import urllib.request
import zlib
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap

from config import Config

COVER_URL = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_600x900.jpg"
#: 兜底：竖版封面缺失时试一下头图
FALLBACK_URL = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"
COVER_W, COVER_H = 200, 300
_USER_AGENT = "SteamBoost/0.1 (+local cache tool)"


def cover_dir() -> Path:
    directory = Config.load().resolved_cover_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class _Signals(QObject):
    done = Signal(str, bool)  # appid, 是否成功


class _DownloadTask(QRunnable):
    """下载一张封面（后台线程执行）。"""

    def __init__(self, appid: str, target: Path, signals: _Signals) -> None:
        super().__init__()
        self.appid = appid
        self.target = target
        self.signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:  # pragma: no cover - 需要网络
        ok = False
        for url in (COVER_URL.format(appid=self.appid), FALLBACK_URL.format(appid=self.appid)):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
                with urllib.request.urlopen(request, timeout=15) as response:
                    data = response.read()
                if len(data) < 512:  # 太小的响应基本是错误页
                    continue
                tmp = self.target.with_suffix(".part")
                tmp.write_bytes(data)
                tmp.replace(self.target)
                ok = True
                break
            except (urllib.error.URLError, OSError, TimeoutError):
                continue
        if not ok:
            try:
                (self.target.parent / f"{self.appid}.fail").write_text("failed", encoding="utf-8")
            except OSError:
                pass
        self.signals.done.emit(self.appid, ok)


class CoverCache(QObject):
    """封面缓存：内存 + 磁盘两级，配一个后台下载线程池。"""

    coverReady = Signal(str)  # appid 封面就绪（主线程信号）

    def __init__(self, parent: QObject | None = None, allow_download: bool = True) -> None:
        super().__init__(parent)
        self.directory = cover_dir()
        self.allow_download = allow_download
        self._memory: dict[str, QPixmap] = {}
        self._pending: set[str] = set()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)
        self._signals = _Signals()
        self._signals.done.connect(self._on_downloaded)

    # ------------------------------------------------------------ 查询
    def get(self, appid: str, name: str = "") -> QPixmap:
        """取封面：内存 → 磁盘 → 请求下载并先返回占位图。永不阻塞。"""
        cached = self._memory.get(appid)
        if cached is not None:
            return cached

        path = self.directory / f"{appid}.jpg"
        if path.is_file():
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    COVER_W, COVER_H, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._memory[appid] = scaled
                return scaled

        self.request(appid)
        return self.placeholder(appid, name)

    def request(self, appid: str) -> None:
        """把封面加入下载队列（已缓存/已排队/已失败则跳过）。"""
        if not self.allow_download or appid in self._pending:
            return
        if (self.directory / f"{appid}.jpg").is_file():
            return
        if (self.directory / f"{appid}.fail").is_file():
            return
        self._pending.add(appid)
        self._pool.start(_DownloadTask(appid, self.directory / f"{appid}.jpg", self._signals))

    def _on_downloaded(self, appid: str, ok: bool) -> None:
        self._pending.discard(appid)
        if ok:
            self.coverReady.emit(appid)

    def is_fully_cached(self, appid: str) -> bool:
        return (self.directory / f"{appid}.jpg").is_file()

    # ------------------------------------------------------------ 占位图
    def placeholder(self, appid: str, name: str) -> QPixmap:
        """现画一张占位封面（渐变 + 名称），并缓存起来。"""
        key = f"__ph__{appid}"
        cached = self._memory.get(key)
        if cached is not None:
            return cached

        pixmap = QPixmap(COVER_W, COVER_H)
        pixmap.fill(QColor("#16202d"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        gradient = QLinearGradient(0, 0, COVER_W, COVER_H)
        # 用 CRC32 而不是"字符码相加"：相邻的 appid（…560 / …570）才会落到不同色相，
        # 否则一整屏占位封面会是同一个颜色。
        seed = zlib.crc32((appid or "0").encode("utf-8")) % 360
        gradient.setColorAt(0.0, QColor.fromHsv(seed, 90, 70))
        gradient.setColorAt(1.0, QColor.fromHsv((seed + 40) % 360, 120, 40))
        painter.fillRect(pixmap.rect(), gradient)

        painter.setPen(QColor("#c7d5e0"))
        font = QFont()
        font.setPointSize(12)
        font.setBold(True)
        painter.setFont(font)
        text_rect = pixmap.rect().adjusted(12, 12, -12, -46)
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap),
            (name or appid)[:40],
        )
        painter.setPen(QColor("#8f98a0"))
        font.setPointSize(9)
        font.setBold(False)
        painter.setFont(font)
        painter.drawText(
            pixmap.rect().adjusted(12, 0, -12, -12),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom),
            f"appid {appid}",
        )
        painter.end()
        self._memory[key] = pixmap
        return pixmap
