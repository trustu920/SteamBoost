"""进度面板：**每个任务一行**，支持排队展示与逐任务取消。

规格要求"每个进行中的任务显示进度条、实时速度、ETA、取消按钮；支持后台排队执行"。
执行层是串行队列（一次只动一个游戏，磁盘 IO 串行更稳），但面板会把
**排队中的任务也列出来**，让用户看到整体进度。

任务状态：排队中 → 进行中 → 完成 / 失败 / 已取消。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui import theme

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_BLOCKED = "blocked"

#: 布局常量的单一来源：面板高度 = 表头 + 可见行数 × 行高
ROW_HEIGHT = 38
HEADER_HEIGHT = 30
MAX_VISIBLE_ROWS = 4

_ICONS = {
    STATE_QUEUED: "◷",
    STATE_RUNNING: "▶",
    STATE_DONE: "✓",
    STATE_FAILED: "✗",
    STATE_BLOCKED: "!",
}


class TaskRow(QFrame):
    """一个任务一行：名称 + 进度条 + 速度/ETA + 取消。"""

    cancelRequested = Signal(str)

    def __init__(self, appid: str, name: str, cancellable: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("TaskRow")
        self.appid = appid
        self.state = STATE_QUEUED
        self.cancellable = cancellable

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        self.icon = QLabel(_ICONS[STATE_QUEUED])
        self.icon.setFixedWidth(16)
        self.name_label = QLabel(name)
        self.name_label.setMinimumWidth(180)
        self.name_label.setMaximumWidth(260)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setMinimumWidth(220)
        self.detail = QLabel("排队中")
        self.detail.setObjectName("Dim")
        self.detail.setMinimumWidth(230)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setFixedWidth(64)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(lambda: self.cancelRequested.emit(self.appid))

        layout.addWidget(self.icon)
        layout.addWidget(self.name_label)
        layout.addWidget(self.bar, 1)
        layout.addWidget(self.detail)
        layout.addWidget(self.cancel_button)

    # ------------------------------------------------------------ 状态
    def set_running(self) -> None:
        self.state = STATE_RUNNING
        self.icon.setText(_ICONS[STATE_RUNNING])
        self.detail.setText("启动中…")
        self.cancel_button.setEnabled(self.cancellable)

    def update_progress(self, progress) -> None:
        self.state = STATE_RUNNING
        self.bar.setValue(int(max(0, min(100, progress.percent))))
        self.detail.setText(
            f"{self._size(progress.bytes_done)} / {self._size(progress.bytes_total)}"
            f"　{progress.speed_text}　剩余 {progress.eta_text}"
        )
        self.cancel_button.setEnabled(self.cancellable)

    def finish(self, state: str, text: str = "") -> None:
        self.state = state
        self.icon.setText(_ICONS.get(state, "?"))
        self.cancel_button.setEnabled(False)
        if state == STATE_DONE:
            self.bar.setValue(100)
            self.detail.setText(text or "完成")
            self.detail.setStyleSheet(f"color: {theme.OK};")
        elif state == STATE_FAILED:
            self.detail.setText(text or "失败")
            self.detail.setStyleSheet(f"color: {theme.DANGER};")
        elif state == STATE_BLOCKED:
            self.detail.setText(text or "已跳过")
            self.detail.setStyleSheet(f"color: {theme.WARN};")
        else:
            self.detail.setText(text or "已取消")

    @staticmethod
    def _size(num: int) -> str:
        from config import human_size

        return human_size(num)


class ProgressPanel(QFrame):
    """进度面板容器；没有任务时显示一行灰色提示。"""

    taskCancelRequested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ProgressPanel")
        self._rows: dict[str, TaskRow] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 6)
        outer.setSpacing(4)

        header = QHBoxLayout()
        self.title = QLabel("任务队列：空闲")
        self.title.setObjectName("Dim")
        header.addWidget(self.title)
        header.addStretch(1)
        self.clear_button = QPushButton("清除已完成")
        self.clear_button.setEnabled(False)
        self.clear_button.clicked.connect(self.clear_finished)
        header.addWidget(self.clear_button)
        outer.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        self.rows_layout = QVBoxLayout(container)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(4)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(container)
        outer.addWidget(self.scroll)
        self.scroll.setVisible(False)

    # ------------------------------------------------------------ 接口
    def add_task(self, appid: str, name: str, cancellable: bool = True) -> TaskRow:
        row = self._rows.get(appid)
        if row is None:
            row = TaskRow(appid, name, cancellable, self)
            row.cancelRequested.connect(self.taskCancelRequested.emit)
            self._rows[appid] = row
            self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
        self._refresh_header()
        return row

    def start_task(self, appid: str) -> None:
        row = self._rows.get(appid)
        if row is not None:
            row.set_running()
            self._refresh_header()

    def update_task(self, appid: str, progress) -> None:
        row = self._rows.get(appid)
        if row is not None:
            row.update_progress(progress)
            self._refresh_header()

    def finish_task(self, appid: str, state: str, text: str = "") -> None:
        row = self._rows.get(appid)
        if row is not None:
            row.finish(state, text)
            self._refresh_header()

    def clear_finished(self) -> None:
        for appid in [key for key, row in self._rows.items() if row.state != STATE_RUNNING]:
            row = self._rows.pop(appid)
            row.setParent(None)
            row.deleteLater()
        self._refresh_header()

    def reset(self) -> None:
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._refresh_header()

    def running_count(self) -> int:
        return sum(1 for row in self._rows.values() if row.state in (STATE_RUNNING, STATE_QUEUED))

    def _refresh_header(self) -> None:
        running = sum(1 for row in self._rows.values() if row.state == STATE_RUNNING)
        queued = sum(1 for row in self._rows.values() if row.state == STATE_QUEUED)
        done = sum(1 for row in self._rows.values() if row.state == STATE_DONE)
        failed = sum(1 for row in self._rows.values() if row.state in (STATE_FAILED, STATE_BLOCKED))
        if not self._rows:
            self.title.setText("任务队列：空闲")
        else:
            parts = []
            if running:
                parts.append(f"进行中 {running}")
            if queued:
                parts.append(f"排队 {queued}")
            if done:
                parts.append(f"完成 {done}")
            if failed:
                parts.append(f"失败/跳过 {failed}")
            self.title.setText("任务队列：" + "　".join(parts))
        self.clear_button.setEnabled(any(row.state != STATE_RUNNING for row in self._rows.values()))

        # 高度按可见行数固定下来：否则外层布局会把面板压扁，任务行只露一半
        visible = min(len(self._rows), MAX_VISIBLE_ROWS)
        self.scroll.setVisible(bool(self._rows))
        if visible:
            self.scroll.setFixedHeight(visible * ROW_HEIGHT + 6)
            self.setFixedHeight(HEADER_HEIGHT + visible * ROW_HEIGHT + 16)
        else:
            self.scroll.setFixedHeight(0)
            self.setFixedHeight(HEADER_HEIGHT + 10)
