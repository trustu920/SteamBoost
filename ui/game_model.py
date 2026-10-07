"""游戏网格的数据模型与卡片绘制（虚拟化）。

为什么用 QListView + 自定义委托，而不是一堆 QWidget 卡片：

* Qt 只为**可见区域**调用 ``paint``，200+ 款游戏也只画屏幕里的那十几张，
  滚动、搜索、排序都不会卡；
* 卡片里的"按钮"是在委托里画出来的胶囊，点击通过 :func:`chip_rects`
  做矩形命中判断——这样按钮也在虚拟化范围内，不为每张卡创建真实控件。

视觉语言（浅色）：白卡片 + 14px 圆角 + 发丝描边 + 手绘柔和阴影；
角标与动作按钮都是胶囊形，颜色取自 :mod:`ui.theme`。
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from steam_scanner import (
    ST_ACCELERATED,
    ST_ACCELERATING,
    ST_ON_HDD,
    ST_UNKNOWN,
    ST_WRITING_BACK,
    GameRecord,
    human_size,
)
from ui import theme
from ui.covers import CoverCache

GameRole = int(Qt.ItemDataRole.UserRole) + 1
CoverRole = int(Qt.ItemDataRole.UserRole) + 2

#: 状态 → 可用动作（键, 按钮文字）
ACTIONS: dict[str, list[tuple[str, str]]] = {
    ST_ON_HDD: [("accelerate", "加速到 SSD")],
    ST_ACCELERATED: [("writeback", "回写母盘"), ("release", "释放空间")],
    ST_WRITING_BACK: [("repair", "修复")],
    ST_ACCELERATING: [("repair", "修复")],
    ST_UNKNOWN: [("repair", "修复")],
}


def actions_for(game: GameRecord) -> list[tuple[str, str]]:
    """返回该游戏当前可执行的动作列表。"""
    return ACTIONS.get(game.status, [])


# ------------------------------------------------------------------ 几何计算
def card_rect(cell: QRect) -> QRect:
    """在单元格里居中放置卡片（视图网格比卡片略大）。"""
    x = cell.x() + max(0, (cell.width() - theme.CARD_W) // 2)
    y = cell.y() + max(0, (cell.height() - theme.CARD_H) // 2)
    return QRect(x, y, theme.CARD_W, theme.CARD_H)


def chip_rects(cell: QRect, count: int) -> list[QRect]:
    """计算动作按钮矩形（与绘制使用同一套算式，保证点击位置与视觉一致）。"""
    if count <= 0:
        return []
    card = card_rect(cell)
    top = card.y() + theme.COVER_H + theme.CHIP_TOP
    available = theme.CARD_W - theme.MARGIN * 2
    width = (available - theme.CHIP_GAP * (count - 1)) // count
    rects: list[QRect] = []
    for index in range(count):
        left = card.x() + theme.MARGIN + index * (width + theme.CHIP_GAP)
        rects.append(QRect(left, top, width, theme.CHIP_H))
    return rects


# ------------------------------------------------------------------ 数据模型
class GameListModel(QAbstractListModel):
    """游戏列表模型；封面由 :class:`~ui.covers.CoverCache` 提供。"""

    def __init__(self, covers: CoverCache, parent=None) -> None:
        super().__init__(parent)
        self.covers = covers
        self._games: list[GameRecord] = []

    # ---------------------------------------------------------- 基本接口
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._games)

    def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)):
        if not index.isValid() or not (0 <= index.row() < len(self._games)):
            return None
        game = self._games[index.row()]
        if role == GameRole:
            return game
        if role == CoverRole:
            return self.covers.get(game.appid, game.name)
        if role == int(Qt.ItemDataRole.DisplayRole):
            return game.name
        if role == int(Qt.ItemDataRole.ToolTipRole):
            return self._tooltip(game)
        return None

    @staticmethod
    def _tooltip(game: GameRecord) -> str:
        lines = [
            f"{game.name}",
            f"appid：{game.appid}",
            f"大小：{human_size(game.size_on_disk)}",
            f"状态：{game.status_label}",
            f"最近游玩：{game.last_played_text}",
            f"位置：{game.library_path}",
        ]
        if game.notes:
            lines.append("备注：" + "；".join(game.notes))
        return "\n".join(lines)

    # ---------------------------------------------------------- 维护接口
    def set_games(self, games: list[GameRecord]) -> None:
        self.beginResetModel()
        self._games = list(games)
        self.endResetModel()

    def games(self) -> list[GameRecord]:
        return list(self._games)

    def game_at(self, row: int) -> GameRecord | None:
        return self._games[row] if 0 <= row < len(self._games) else None

    def refresh_cover(self, appid: str) -> None:
        """某张封面下载完成后，只刷新对应行。"""
        for row, game in enumerate(self._games):
            if game.appid == appid:
                index = self.index(row, 0)
                self.dataChanged.emit(index, index, [CoverRole])
                return


# ------------------------------------------------------------------ 卡片绘制
class GameCardDelegate(QStyledItemDelegate):
    """绘制一张游戏卡片：阴影 + 白底 + 圆角封面 + 名称 + 胶囊角标 + 胶囊动作按钮。"""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(theme.CARD_W + theme.GRID_PAD, theme.CARD_H + theme.GRID_PAD)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        game: GameRecord | None = index.data(GameRole)
        if game is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        cell = option.rect
        card = card_rect(cell)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        # 1) 柔和阴影：手绘几层低透明度圆角矩形（样式表不支持 box-shadow）
        painter.setPen(Qt.PenStyle.NoPen)
        for offset, alpha in ((0, 5), (2, 7), (5, 9)):
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(
                card.adjusted(-1, offset, 1, offset + 3), theme.CARD_RADIUS, theme.CARD_RADIUS
            )

        # 2) 卡片底：白底 + 发丝描边（选中时换成强调色）
        painter.setBrush(QColor(theme.BG_DARK))
        painter.setPen(QPen(QColor(theme.ACCENT if selected else "#000000"),
                            2 if selected else 1))
        border_color = QColor(theme.ACCENT) if selected else QColor(0, 0, 0, 20)
        painter.setPen(QPen(border_color, 2 if selected else 1))
        painter.drawRoundedRect(card.adjusted(0, 0, -1, -1), theme.CARD_RADIUS, theme.CARD_RADIUS)
        if hovered and not selected:
            painter.setBrush(QColor(0, 0, 0, 6))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(card.adjusted(1, 1, -2, -2), theme.CARD_RADIUS, theme.CARD_RADIUS)

        # 3) 封面：只把上面两个角做成圆角，与卡片贴合
        cover = index.data(CoverRole)
        cover_rect = QRect(card.x(), card.y(), theme.CARD_W, theme.COVER_H)
        if cover is not None and not cover.isNull():
            radius = theme.CARD_RADIUS
            path = QPainterPath()
            x, y, w, h = cover_rect.x(), cover_rect.y(), cover_rect.width(), cover_rect.height()
            path.moveTo(x, y + h)
            path.lineTo(x, y + radius)
            path.quadTo(x, y, x + radius, y)
            path.lineTo(x + w - radius, y)
            path.quadTo(x + w, y, x + w, y + radius)
            path.lineTo(x + w, y + h)
            path.closeSubpath()
            painter.save()
            painter.setClipPath(path)
            source_w = min(cover.width(), int(cover.height() * theme.CARD_W / theme.COVER_H))
            source_h = int(source_w * theme.COVER_H / theme.CARD_W)
            source_x = (cover.width() - source_w) // 2
            source_y = (cover.height() - source_h) // 2
            painter.drawPixmap(cover_rect, cover, QRect(source_x, source_y, source_w, source_h))
            painter.restore()

        # 4) 名称
        name_font = QFont()
        name_font.setPointSize(10)
        name_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(name_font)
        painter.setPen(QColor(theme.TEXT))
        metrics = QFontMetrics(name_font)
        name = metrics.elidedText(game.name, Qt.TextElideMode.ElideRight, theme.CARD_W - theme.MARGIN * 2)
        painter.drawText(
            QRect(card.x() + theme.MARGIN, card.y() + theme.COVER_H + theme.NAME_TOP,
                  theme.CARD_W - theme.MARGIN * 2, 20),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            name,
        )

        # 5) 状态角标（胶囊）+ 大小
        small = QFont()
        small.setPointSize(8)
        painter.setFont(small)
        small_metrics = QFontMetrics(small)
        label, badge_bg, badge_fg = theme.badge_color(game.status)
        badge_w = small_metrics.horizontalAdvance(label) + 18
        badge = QRect(card.x() + theme.MARGIN, card.y() + theme.COVER_H + theme.BADGE_TOP, badge_w, 19)
        painter.setBrush(QColor(badge_bg))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(badge, 9, 9)
        painter.setPen(QColor(badge_fg))
        painter.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), label)

        painter.setPen(QColor(theme.TEXT_DIM))
        painter.drawText(
            QRect(card.x() + theme.MARGIN + badge_w + 8, card.y() + theme.COVER_H + theme.BADGE_TOP,
                  theme.CARD_W - theme.MARGIN * 2 - badge_w - 8, 19),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            human_size(game.size_on_disk),
        )

        # 6) 动作按钮（胶囊）
        chip_font = QFont()
        chip_font.setPointSize(8)
        chip_font.setWeight(QFont.Weight.Medium)
        actions = actions_for(game)
        for rect, (key, text) in zip(chip_rects(cell, len(actions)), actions):
            danger = key == "release"
            painter.setBrush(QColor(theme.DANGER_TINT if danger else theme.ACCENT_TINT))
            painter.setPen(QPen(QColor(255, 59, 48, 60) if danger else QColor(0, 113, 227, 60), 1))
            painter.drawRoundedRect(rect, theme.CHIP_H / 2, theme.CHIP_H / 2)
            painter.setPen(QColor(theme.DANGER_DARK if danger else theme.ACCENT))
            painter.setFont(chip_font)
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), text)

        painter.restore()
