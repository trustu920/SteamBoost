"""确认对话框：删除确认、多余文件搬移确认。

设计原则（用户明确要求"不可以误删我的文件"）
--------------------------------------------
* 对话框把**确切路径、文件数、字节数、样例**全部摊开；
* 「删除」按钮**默认禁用**，必须勾选"我已核对上述路径"才可点击——
  这是一个刻意的第二步动作，防手滑；
* 回车键绑定到「取消」，不是「删除」；
* 危险按钮用红色，且与取消按钮拉开距离。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
)

from config import human_size
from deletion_guard import DeletionRequest
from ui import theme


class ConfirmDeletionDialog(QDialog):
    """删除前的最后一道人工确认。"""

    def __init__(self, request: DeletionRequest, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("确认删除")
        self.setMinimumWidth(620)
        self.request = request
        self._build()
        self.setStyleSheet(theme.STYLE_SHEET)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        headline = QLabel("即将删除以下内容，请仔细核对")
        headline.setStyleSheet(f"color: {theme.DANGER}; font-size: 15px; font-weight: 600;")
        layout.addWidget(headline)

        path_label = QLabel(self.request.target)
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path_label.setStyleSheet(
            f"background: {theme.BG_DEEP}; border: 1px solid {theme.BORDER};"
            f" border-radius: 3px; padding: 8px; color: {theme.TEXT};"
        )
        layout.addWidget(path_label)

        info = QLabel(
            f"类型：{self.request.kind_label}\n"
            f"内容：{self.request.files} 个文件 / {human_size(self.request.bytes)}\n"
            f"原因：{self.request.reason or '（未说明）'}\n"
            f"可撤销：{'是' if self.request.reversible else '否，删除后无法恢复'}"
        )
        info.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(info)

        if self.request.sample:
            sample = QListWidget()
            sample.setMaximumHeight(120)
            sample.addItems(self.request.sample)
            if self.request.files > len(self.request.sample):
                sample.addItem(f"… 其余 {self.request.files - len(self.request.sample)} 个文件")
            layout.addWidget(QLabel("包含的文件（样例）："))
            layout.addWidget(sample)

        self.confirm_check = QCheckBox("我已核对上述路径，确认删除")
        self.confirm_check.stateChanged.connect(self._on_check)
        layout.addWidget(self.confirm_check)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消（默认）")
        self.cancel_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        self.delete_button = QPushButton("删除")
        self.delete_button.setObjectName("Danger")
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self.accept)
        buttons.addWidget(self.cancel_button)
        buttons.addSpacing(12)
        buttons.addWidget(self.delete_button)
        layout.addLayout(buttons)

    def _on_check(self, _state: int) -> None:
        self.delete_button.setEnabled(self.confirm_check.isChecked())


class ConfirmExtrasDialog(QDialog):
    """回写前确认：把母盘上多出来的文件搬进隔离区（不是删除）。"""

    def __init__(self, game_name: str, extras: list[str], total_bytes: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("回写前确认")
        self.setMinimumWidth(620)
        self._build(game_name, extras, total_bytes)
        self.setStyleSheet(theme.STYLE_SHEET)

    def _build(self, game_name: str, extras: list[str], total_bytes: int) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        headline = QLabel(f"【{game_name}】回写前需要先把母盘上多出来的文件搬进隔离区")
        headline.setStyleSheet(f"color: {theme.WARN}; font-size: 15px; font-weight: 600;")
        headline.setWordWrap(True)
        layout.addWidget(headline)

        explain = QLabel(
            f"数量：{len(extras)} 个条目，合计 {human_size(total_bytes)}\n"
            "这些文件**不会被删除**，只是移动到隔离区；\n"
            "等你确认游戏能正常运行之后，再决定是否删除它们，也可以随时还原。"
        )
        explain.setStyleSheet(f"background: {theme.BG_DEEP}; border: 1px solid {theme.BORDER};"
                              f" border-radius: 3px; padding: 8px; color: {theme.TEXT_DIM};")
        explain.setWordWrap(True)
        layout.addWidget(explain)

        listing = QListWidget()
        listing.addItems(extras[:200])
        if len(extras) > 200:
            listing.addItem(f"… 其余 {len(extras) - 200} 个")
        layout.addWidget(QLabel("将被搬移的条目："))
        layout.addWidget(listing)

        buttons = QDialogButtonBox()
        cancel = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        proceed = buttons.addButton("继续回写", QDialogButtonBox.ButtonRole.AcceptRole)
        proceed.setObjectName("Primary")
        cancel.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
