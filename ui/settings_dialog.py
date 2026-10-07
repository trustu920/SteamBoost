"""设置页：两个盘的选择、缓存目录、引擎与校验、日志占用与清理。

按既定设计：**只让用户选两个盘**（母盘 / 加速盘），程序不判断磁盘介质；
缓存目录与这两个盘上的 Steam 库做校验，重叠即拒绝。
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from config import Config, default_cache_dir, human_size, list_volumes, normalize_drive
from copy_engine import FASTCOPY_NAMES, find_fastcopy
from logger import log_usage
from ui import theme


class SettingsDialog(QDialog):
    """设置对话框；确认后把新的 :class:`Config` 交给调用方保存。"""

    pruneLogsRequested = Signal()

    def __init__(self, cfg: Config, libraries: list[str] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(640)
        self.cfg = Config(**cfg.to_dict())
        self.libraries = libraries or []
        self._build()
        self.setStyleSheet(theme.STYLE_SHEET)

    # ------------------------------------------------------------ 构建
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("两个盘")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(8)

        self.mother_combo = QComboBox()
        self.cache_combo = QComboBox()
        for combo in (self.mother_combo, self.cache_combo):
            combo.addItem("— 未选择 —", "")
        for volume in list_volumes():
            text = (
                f"{volume['drive']}:　可用 {human_size(volume['free'])} / {human_size(volume['total'])}"
            )
            self.mother_combo.addItem(text, volume["drive"])
            self.cache_combo.addItem(text, volume["drive"])
        self._select(self.mother_combo, self.cfg.mother_drive)
        self._select(self.cache_combo, self.cfg.cache_drive)
        form.addRow("母盘（存放游戏母本）", self.mother_combo)
        form.addRow("加速盘（存放缓存副本）", self.cache_combo)

        cache_row = QHBoxLayout()
        self.cache_dir_edit = QLineEdit(self.cfg.cache_dir)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_cache_dir)
        cache_row.addWidget(self.cache_dir_edit, 1)
        cache_row.addWidget(browse)
        form.addRow("缓存目录", cache_row)
        # 占位提示跟着所选加速盘走，不写死任何盘符
        self.cache_combo.currentIndexChanged.connect(self._refresh_cache_placeholder)
        self._refresh_cache_placeholder()
        layout.addLayout(form)

        hint = QLabel(
            "说明：母盘与加速盘不能是同一个盘；缓存目录不能位于任何 Steam 库内部。"
            "程序不检测磁盘类型，由你指定哪个是固态盘。"
        )
        hint.setObjectName("Dim")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        title2 = QLabel("复制与校验")
        title2.setObjectName("SectionTitle")
        layout.addWidget(title2)

        form2 = QFormLayout()
        form2.setSpacing(8)
        self.engine_combo = QComboBox()
        self.engine_combo.setMinimumWidth(360)
        self.engine_combo.addItem("FastCopy（首选，差异复制 / 镜像回写）", "fastcopy")
        self.engine_combo.addItem("robocopy（Windows 自带，回退方案）", "robocopy")
        self._select(self.engine_combo, self.cfg.copy_engine)
        form2.addRow("复制引擎", self.engine_combo)

        fastcopy_row = QHBoxLayout()
        self.fastcopy_edit = QLineEdit(self.cfg.fastcopy_path)
        self.fastcopy_edit.setPlaceholderText("留空 = 自动查找")
        browse_fc = QPushButton("浏览…")
        browse_fc.clicked.connect(self._browse_fastcopy)
        detect_fc = QPushButton("自动检测")
        detect_fc.clicked.connect(self._autodetect_fastcopy)
        fastcopy_row.addWidget(self.fastcopy_edit, 1)
        fastcopy_row.addWidget(browse_fc)
        fastcopy_row.addWidget(detect_fc)
        form2.addRow("FastCopy 路径", fastcopy_row)

        self.fastcopy_hint = QLabel("")
        self.fastcopy_hint.setWordWrap(True)
        form2.addRow("", self.fastcopy_hint)
        self.fastcopy_edit.textChanged.connect(self._refresh_fastcopy_hint)
        self.engine_combo.currentIndexChanged.connect(self._refresh_fastcopy_hint)
        self._refresh_fastcopy_hint()

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 128)
        self.threads_spin.setValue(int(self.cfg.robocopy_threads))
        self.threads_spin.setMaximumWidth(120)
        form2.addRow("robocopy 线程数", self.threads_spin)

        self.verify_check = QCheckBox("启用 FastCopy 的 xxHash3 回读校验（更稳，速度略降）")
        self.verify_check.setChecked(bool(self.cfg.fastcopy_verify))
        form2.addRow("", self.verify_check)

        self.tray_check = QCheckBox("关闭窗口时最小化到系统托盘")
        self.tray_check.setChecked(bool(self.cfg.minimize_to_tray))
        form2.addRow("", self.tray_check)
        layout.addLayout(form2)

        title3 = QLabel("提醒阈值")
        title3.setObjectName("SectionTitle")
        layout.addWidget(title3)
        form3 = QFormLayout()
        self.percent_spin = QSpinBox()
        self.percent_spin.setRange(1, 90)
        self.percent_spin.setValue(int(self.cfg.free_space_min_percent))
        self.percent_spin.setSuffix(" %")
        self.percent_spin.setMaximumWidth(120)
        self.gb_spin = QSpinBox()
        self.gb_spin.setRange(1, 5000)
        self.gb_spin.setValue(int(self.cfg.free_space_min_gb))
        self.gb_spin.setSuffix(" GB")
        self.gb_spin.setMaximumWidth(120)
        form3.addRow("加速盘剩余低于", self.percent_spin)
        form3.addRow("或低于", self.gb_spin)
        layout.addLayout(form3)

        title4 = QLabel("日志")
        title4.setObjectName("SectionTitle")
        layout.addWidget(title4)
        log_row = QHBoxLayout()
        count, total = log_usage(self.cfg)
        self.log_label = QLabel(f"{self.cfg.resolved_log_dir()}　（{count} 个文件 / {human_size(total)}）")
        self.log_label.setObjectName("Dim")
        self.log_label.setWordWrap(True)
        prune = QPushButton("清理 30 天前的日志")
        prune.clicked.connect(self.pruneLogsRequested.emit)
        log_row.addWidget(self.log_label, 1)
        log_row.addWidget(prune)
        layout.addLayout(log_row)
        note = QLabel(
            "日志按天滚动：调试日志保留 14 天、操作审计日志保留 90 天，超期由日志库自动清理。"
            "上面的按钮是手动清理，会先让你确认每一个要删的文件。"
        )
        note.setObjectName("Dim")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox()
        save = buttons.addButton("保存", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setObjectName("Primary")
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _select(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _browse_cache_dir(self) -> None:
        start = self.cache_dir_edit.text().strip()
        if not start:
            drive = normalize_drive(self.cache_combo.currentData() or "")
            # 默认落在所选加速盘根目录；没选盘就退回用户主目录（不写死任何盘符）
            start = f"{drive}:\\" if drive else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "选择缓存目录", start)
        if chosen:
            self.cache_dir_edit.setText(str(Path(chosen)))

    def _refresh_cache_placeholder(self) -> None:
        """缓存目录的占位提示按所选加速盘给出建议值。"""
        drive = normalize_drive(self.cache_combo.currentData() or "")
        if not self.cache_dir_edit.text().strip():
            self.cache_dir_edit.setPlaceholderText(f"例如 {default_cache_dir(drive)}")

    def _browse_fastcopy(self) -> None:
        start = self.fastcopy_edit.text().strip()
        if start:
            start = str(Path(start).parent)
        else:
            found = find_fastcopy()
            start = str(found.parent if found else Path(os.environ.get("ProgramFiles", "")))
        chosen, _filter = QFileDialog.getOpenFileName(
            self, "选择 FastCopy 命令行程序（fcp.exe）", start,
            "FastCopy 命令行 (fcp.exe);;FastCopy 主程序 (FastCopy.exe);;可执行文件 (*.exe)",
        )
        if chosen:
            self.fastcopy_edit.setText(str(Path(chosen)))

    def _autodetect_fastcopy(self) -> None:
        """重新自动查找一次，并把结果写进输入框。"""
        found = find_fastcopy()
        self.fastcopy_edit.setText(str(found) if found else "")
        self._refresh_fastcopy_hint()

    def _refresh_fastcopy_hint(self) -> None:
        """把"实际会用哪个引擎"直白地写在界面上，避免静默降级。"""
        wants_fastcopy = (self.engine_combo.currentData() or "fastcopy") == "fastcopy"
        explicit = self.fastcopy_edit.text().strip()
        found = find_fastcopy(explicit)

        if not wants_fastcopy:
            self._set_hint("已选择 robocopy（Windows 自带），不会调用 FastCopy。", "Dim")
            return
        if found is None:
            where = f"（你指定的路径无效：{explicit}）" if explicit else ""
            self._set_hint(
                f"✖ 未找到 FastCopy{where}，加速时会自动回退到 robocopy（速度较慢）。"
                f"请填写 {' 或 '.join(FASTCOPY_NAMES)} 的完整路径。",
                "Bad",
            )
            return
        source = "你指定的路径" if explicit else "自动查找"
        self._set_hint(f"✔ 已找到（{source}）：{found}", "Ok")

    def _set_hint(self, text: str, object_name: str) -> None:
        self.fastcopy_hint.setText(text)
        if self.fastcopy_hint.objectName() != object_name:
            self.fastcopy_hint.setObjectName(object_name)
            # objectName 变了要重新上样式，否则颜色不跟着变
            self.fastcopy_hint.style().unpolish(self.fastcopy_hint)
            self.fastcopy_hint.style().polish(self.fastcopy_hint)

    # ------------------------------------------------------------ 取值
    def result_config(self) -> Config:
        """把界面上的选择写回配置对象。"""
        self.cfg.mother_drive = normalize_drive(self.mother_combo.currentData() or "")
        self.cfg.cache_drive = normalize_drive(self.cache_combo.currentData() or "")
        self.cfg.cache_dir = self.cache_dir_edit.text().strip()
        self.cfg.copy_engine = self.engine_combo.currentData() or "fastcopy"
        self.cfg.fastcopy_path = self.fastcopy_edit.text().strip()
        self.cfg.robocopy_threads = int(self.threads_spin.value())
        self.cfg.fastcopy_verify = bool(self.verify_check.isChecked())
        self.cfg.minimize_to_tray = bool(self.tray_check.isChecked())
        self.cfg.free_space_min_percent = float(self.percent_spin.value())
        self.cfg.free_space_min_gb = float(self.gb_spin.value())
        return self.cfg

    def refresh_log_usage(self) -> None:
        count, total = log_usage(self.cfg)
        self.log_label.setText(f"{self.cfg.resolved_log_dir()}　（{count} 个文件 / {human_size(total)}）")
