"""修复向导界面：把 :mod:`repair` 分析出的现场逐条呈现，用户选动作再执行。

交互原则
--------
* 每条现场都写清楚"现在磁盘上是什么状态"，不吓唬人也不隐瞒；
* 动作按危险性标注：删除类动作会走 :mod:`deletion_guard` 的确认框；
* 执行放在后台线程（"继续加速"可能要复制几十 GB），界面不冻结；
* 执行完就地刷新，不需要重启程序。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from config import Config
from repair import RepairCase, RepairFailed, apply
from state import StateStore
from ui import theme
from ui.workers import RepairWorker


class RepairDialog(QDialog):
    """修复向导主窗口。"""

    repaired = Signal(str)   # 结果说明（外层据此重新扫描）

    def __init__(
        self,
        cases: list[RepairCase],
        cfg: Config,
        store: StateStore,
        report=None,
        deletion_confirmer=None,
        extras_confirmer=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("修复向导")
        self.setMinimumSize(880, 560)
        self.cases = list(cases)
        self.cfg = cfg
        self.store = store
        self.report = report
        self.deletion_confirmer = deletion_confirmer
        self.extras_confirmer = extras_confirmer
        self.worker: RepairWorker | None = None
        self._build()
        self._refresh_list()
        self.setStyleSheet(theme.STYLE_SHEET)

    # ------------------------------------------------------------ 构建
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        headline = QLabel("检测到需要处理的情况")
        headline.setObjectName("SectionTitle")
        outer.addWidget(headline)
        note = QLabel(
            "下面每一条都说明了磁盘上的实际状态。程序不会自动改动任何东西——"
            "请你选择处理方式；涉及删除时会再弹一次确认框。"
        )
        note.setObjectName("Dim")
        note.setWordWrap(True)
        outer.addWidget(note)

        body = QHBoxLayout()
        body.setSpacing(10)

        self.list = QListWidget()
        self.list.setMaximumWidth(280)
        self.list.currentRowChanged.connect(self._show_case)
        body.addWidget(self.list)

        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail = QFrame()
        self.detail.setObjectName("Card")
        self.detail_layout = QVBoxLayout(self.detail)
        self.detail_layout.setContentsMargins(16, 14, 16, 14)
        self.detail_layout.setSpacing(10)
        self.detail_scroll.setWidget(self.detail)
        body.addWidget(self.detail_scroll, 1)
        outer.addLayout(body, 1)

        footer = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("Dim")
        footer.addWidget(self.status, 1)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.accept)
        footer.addWidget(self.close_button)
        outer.addLayout(footer)

    def _refresh_list(self) -> None:
        self.list.clear()
        for case in self.cases:
            item = QListWidgetItem(case.title)
            item.setToolTip(case.detail)
            self.list.addItem(item)
        if self.cases:
            self.list.setCurrentRow(0)
        else:
            self._show_empty()

    def _show_empty(self) -> None:
        self._clear_detail()
        label = QLabel("没有需要处理的情况，一切正常。")
        label.setStyleSheet(f"color: {theme.OK}; font-size: 15px;")
        self.detail_layout.addWidget(label)
        self.detail_layout.addStretch(1)

    def _clear_detail(self) -> None:
        while self.detail_layout.count():
            item = self.detail_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    # ------------------------------------------------------------ 展示
    def _show_case(self, row: int) -> None:
        self._clear_detail()
        if not (0 <= row < len(self.cases)):
            return
        case = self.cases[row]

        title = QLabel(case.title)
        title.setStyleSheet(f"font-size: 15px; font-weight: 600; color: {theme.TEXT};")
        title.setWordWrap(True)
        self.detail_layout.addWidget(title)

        detail = QLabel(case.detail)
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail.setStyleSheet(
            f"background: {theme.BG_DEEP}; border: 1px solid {theme.BORDER_SOFT};"
            f" border-radius: 10px; padding: 10px; color: {theme.TEXT_DIM};"
        )
        self.detail_layout.addWidget(detail)

        self.detail_layout.addWidget(QLabel("你可以这样处理："))
        for action in case.actions:
            row_widget = QFrame()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(10)

            button = QPushButton(action.label)
            if action.recommended:
                button.setObjectName("Primary")
            elif action.destructive:
                button.setObjectName("Danger")
            button.setMinimumWidth(190)
            button.clicked.connect(lambda _checked=False, c=case, a=action: self._run(c, a.key))

            text = QLabel(action.detail)
            text.setObjectName("Dim")
            text.setWordWrap(True)

            row_layout.addWidget(button)
            row_layout.addWidget(text, 1)
            self.detail_layout.addWidget(row_widget)

        self.detail_layout.addStretch(1)

    # ------------------------------------------------------------ 执行
    def _run(self, case: RepairCase, action_key: str) -> None:
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "请稍候", "上一个修复动作还在执行。")
            return
        # 破坏性动作在执行前给出一次明确的界面确认（真正的删除还会再确认一次）
        destructive = case.action(action_key)
        if destructive is not None and destructive.destructive:
            answer = QMessageBox.question(
                self,
                "确认执行",
                f"{destructive.label}\n\n{case.title}\n\n是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._set_busy(True, f"正在执行：{destructive.label if destructive else action_key}…")
        worker = RepairWorker(
            case, action_key, self.cfg, self.store, self.report,
            deletion_confirmer=self.deletion_confirmer,
            extras_confirmer=self.extras_confirmer,
            parent=self,
        )
        worker.finishedOk.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        self.worker = worker
        worker.start()

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self.close_button.setEnabled(not busy)
        self.list.setEnabled(not busy)
        self.status.setText(text)

    def _on_done(self, message: str) -> None:
        self._set_busy(False, message)
        QMessageBox.information(self, "处理完成", message)
        self.repaired.emit(message)
        self._drop_current_case()

    def _on_failed(self, message: str) -> None:
        self._set_busy(False, "执行失败，未做进一步改动")
        hint = ""
        if isinstance(message, str) and "未确认" in message:
            hint = "\n\n（你在确认框里选择了取消，因此没有执行删除。）"
        QMessageBox.warning(self, "处理失败", f"{message}{hint}")

    def _drop_current_case(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self.cases):
            self.cases.pop(row)
            self._refresh_list()
        if not self.cases:
            self.status.setText("全部处理完毕")
