"""空间提醒：加速盘剩余空间低于阈值时，列出"最该释放"的游戏。

策略（对应需求"SSD 空间智能提醒"）
--------------------------------
* 阈值：``剩余 < 总容量 × 百分比`` 或 ``剩余 < 固定 GB``，满足任一即提醒；
* 候选：当前处于"已加速"状态的游戏，**按最近游玩时间从旧到新**排序
  （最久没玩的排最前，也就是最该释放的）；
* 默认勾选：从最旧开始累加，直到释放出来的空间足以回到阈值以上；
* 用户确认后才执行释放，程序不会自作主张。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from config import Config, human_size, volume_free_bytes, volume_total_bytes
from steam_scanner import RELEASABLE, GameRecord, human_size as _human_size  # noqa: F401
from ui import theme


@dataclass
class SpaceStatus:
    """加速盘空间状况。"""

    drive: str
    free: int
    total: int
    threshold: int
    reason: str

    @property
    def free_text(self) -> str:
        return human_size(self.free)

    @property
    def threshold_text(self) -> str:
        return human_size(self.threshold)

    def describe(self) -> str:
        percent = (self.free / self.total * 100) if self.total else 0
        return (
            f"加速盘 {self.drive}: 剩余 {self.free_text}（{percent:.1f}%），"
            f"已低于提醒阈值 {self.threshold_text}（{self.reason}）"
        )


def check_space(cfg: Config) -> SpaceStatus | None:
    """检查加速盘空间；不需要提醒时返回 None。"""
    drive = (cfg.cache_drive or "").strip().upper()
    if not drive:
        return None
    total = volume_total_bytes(drive)
    free = volume_free_bytes(drive)
    if total <= 0 or free < 0:
        return None

    percent_limit = int(total * max(0.0, cfg.free_space_min_percent) / 100.0)
    gb_limit = int(max(0.0, cfg.free_space_min_gb) * 1024 ** 3)
    threshold = max(percent_limit, gb_limit)
    if threshold <= 0 or free >= threshold:
        return None

    if percent_limit >= gb_limit:
        reason = f"低于 {cfg.free_space_min_percent:.0f}% 阈值"
    else:
        reason = f"低于 {cfg.free_space_min_gb:.0f} GB 阈值"
    return SpaceStatus(drive=drive, free=free, total=total, threshold=threshold, reason=reason)


def release_candidates(games: list[GameRecord]) -> list[GameRecord]:
    """已加速且可释放的游戏，按最近游玩从旧到新（没玩过的排最前）。"""
    items = [game for game in games if game.status in RELEASABLE]
    return sorted(items, key=lambda game: (game.last_played or 0, game.name.lower()))


def suggest_selection(candidates: list[GameRecord], status: SpaceStatus) -> list[str]:
    """从最旧的开始累加，直到释放量足以回到阈值以上（至少选一个）。"""
    need = max(0, status.threshold - status.free)
    picked: list[str] = []
    freed = 0
    for game in candidates:
        picked.append(game.appid)
        freed += int(game.size_on_disk or 0)
        if freed >= need:
            break
    return picked


class SpaceWarningDialog(QDialog):
    """空间不足提醒：列出可释放的游戏，用户勾选后一键释放。"""

    releaseRequested = Signal(list)   # [appid, ...]
    snoozeRequested = Signal()

    def __init__(self, status: SpaceStatus, candidates: list[GameRecord], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("加速盘空间不足")
        self.setMinimumSize(760, 460)
        self.status = status
        self.candidates = list(candidates)
        self._build()
        self.setStyleSheet(theme.STYLE_SHEET)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        headline = QLabel(self.status.describe())
        headline.setStyleSheet(f"color: {theme.DANGER}; font-size: 15px; font-weight: 600;")
        headline.setWordWrap(True)
        layout.addWidget(headline)

        hint = QLabel(
            "下面按「最近游玩」从旧到新列出加速盘上的游戏——越靠上越久没玩，越适合释放。\n"
            "已按需要释放的空间预勾选，你可以自由增减；释放前仍会逐步确认。"
        )
        hint.setObjectName("Dim")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["释放", "游戏", "占用", "最近游玩"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        # 摘要标签必须先于填表创建：填表会调用 _update_summary()
        self.summary = QLabel("")
        self.summary.setObjectName("Dim")
        layout.addWidget(self.summary)

        row = QHBoxLayout()
        row.addStretch(1)
        self.snooze_button = QPushButton("本次运行不再提醒")
        self.snooze_button.clicked.connect(self._snooze)
        self.release_button = QPushButton("释放选中")
        self.release_button.setObjectName("Danger")
        self.release_button.clicked.connect(self._release)
        close = QPushButton("稍后再说")
        close.clicked.connect(self.reject)
        row.addWidget(self.snooze_button)
        row.addWidget(close)
        row.addWidget(self.release_button)
        layout.addLayout(row)

        # 最后再填表：填表 → _update_summary() 会用到上面的摘要与按钮
        self._fill_table()

    def _fill_table(self) -> None:
        suggested = set(suggest_selection(self.candidates, self.status))
        self.table.setRowCount(len(self.candidates))
        for row, game in enumerate(self.candidates):
            box = QCheckBox()
            box.setChecked(game.appid in suggested)
            box.stateChanged.connect(self._update_summary)
            holder = QHBoxLayout()
            holder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            holder.setContentsMargins(0, 0, 0, 0)
            from PySide6.QtWidgets import QWidget

            container = QWidget()
            holder.addWidget(box)
            container.setLayout(holder)
            self.table.setCellWidget(row, 0, container)

            name = QTableWidgetItem(game.name)
            name.setData(Qt.ItemDataRole.UserRole, game.appid)
            name.setToolTip(game.game_path)
            self.table.setItem(row, 1, name)
            self.table.setItem(row, 2, QTableWidgetItem(human_size(game.size_on_disk)))
            self.table.setItem(row, 3, QTableWidgetItem(game.last_played_text))
        self._update_summary()

    def _checked_appids(self) -> list[str]:
        picked: list[str] = []
        for row in range(self.table.rowCount()):
            container = self.table.cellWidget(row, 0)
            if container is None:
                continue
            from PySide6.QtWidgets import QCheckBox as _Box

            box = container.findChild(_Box)
            item = self.table.item(row, 1)
            if box is not None and box.isChecked() and item is not None:
                picked.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return picked

    def _update_summary(self) -> None:
        picked = set(self._checked_appids())
        freed = sum(int(game.size_on_disk or 0) for game in self.candidates if game.appid in picked)
        after = self.status.free + freed
        self.summary.setText(
            f"已选 {len(picked)} 项，可释放 {human_size(freed)}；"
            f"释放后加速盘剩余约 {human_size(after)}（阈值 {self.status.threshold_text}）"
        )
        self.release_button.setEnabled(bool(picked))

    def _release(self) -> None:
        picked = self._checked_appids()
        if picked:
            self.releaseRequested.emit(picked)
            self.accept()

    def _snooze(self) -> None:
        self.snoozeRequested.emit()
        self.reject()
