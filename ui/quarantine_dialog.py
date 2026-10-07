"""隔离区管理对话框。

隔离区里放的是"回写时从母盘搬出来的多余文件"——**没有被删除**，
所以这里提供两个动作：

* **还原**：搬回母盘原位置（不涉及删除，直接执行）；
* **删除**：真正释放空间，走删除闸门 + 界面确认（默认禁用，需勾选核对）。

对话框只负责收集意图，真正的删除由控制层通过 :mod:`quarantine` 执行。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from config import human_size
from quarantine import QuarantineItem
from ui import theme


class QuarantineDialog(QDialog):
    """列出隔离项，允许还原或（确认后）删除。"""

    purgeRequested = Signal(str)    # item_dir
    restoreRequested = Signal(str)  # item_dir

    def __init__(self, items: list[QuarantineItem], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("隔离区")
        self.setMinimumSize(760, 460)
        self._items = list(items)
        self._build()
        self.reload(self._items)
        self.setStyleSheet(theme.STYLE_SHEET)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        headline = QLabel("回写时从母盘搬出来的文件（未被删除）")
        headline.setObjectName("SectionTitle")
        layout.addWidget(headline)

        explain = QLabel(
            "请先启动游戏确认一切正常，再决定是否删除这些文件。\n"
            "删除会真正释放空间且不可恢复；也可以先还原它们。"
        )
        explain.setObjectName("Dim")
        explain.setWordWrap(True)
        layout.addWidget(explain)

        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.list, 1)

        self.summary = QLabel("")
        self.summary.setObjectName("Dim")
        layout.addWidget(self.summary)

        row = QHBoxLayout()
        row.addStretch(1)
        self.restore_button = QPushButton("还原选中")
        self.restore_button.clicked.connect(self._restore)
        self.purge_button = QPushButton("删除选中（需确认）")
        self.purge_button.setObjectName("Danger")
        self.purge_button.clicked.connect(self._purge)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(self.restore_button)
        row.addWidget(self.purge_button)
        row.addWidget(close)
        layout.addLayout(row)

    # ------------------------------------------------------------ 数据
    def reload(self, items: list[QuarantineItem]) -> None:
        self._items = list(items)
        self.list.clear()
        for item in self._items:
            text = (
                f"{item.created_text}　{item.appid} {item.name}　"
                f"{item.files} 个文件 / {item.size_text}"
            )
            entry = QListWidgetItem(text)
            entry.setData(0x0100, item.item_dir)  # Qt.UserRole
            entry.setToolTip(item.item_dir)
            self.list.addItem(entry)
        total = sum(item.bytes for item in self._items)
        self.summary.setText(
            f"共 {len(self._items)} 项 / {human_size(total)}"
            if self._items
            else "隔离区为空"
        )
        self._update_buttons()

    def selected_dirs(self) -> list[str]:
        return [entry.data(0x0100) for entry in self.list.selectedItems()]

    def _update_buttons(self) -> None:
        has_selection = bool(self.list.selectedItems())
        self.restore_button.setEnabled(has_selection)
        self.purge_button.setEnabled(has_selection)

    # ------------------------------------------------------------ 动作
    def _purge(self) -> None:
        for item_dir in self.selected_dirs():
            self.purgeRequested.emit(item_dir)

    def _restore(self) -> None:
        for item_dir in self.selected_dirs():
            self.restoreRequested.emit(item_dir)
