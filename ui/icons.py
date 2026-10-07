"""应用图标：现画（不依赖外部资源文件）。

一个圆角方块 + 向上箭头，代表"把游戏加速到固态盘"。
主窗口左上角与系统托盘共用同一份绘制代码。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPixmap, QPolygon

from ui import theme


def app_pixmap(size: int = 64) -> QPixmap:
    """画一个 ``size × size`` 的圆角图标。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    radius = max(6, int(size * 0.24))
    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0.0, QColor("#3d9bff"))
    gradient.setColorAt(1.0, QColor(theme.ACCENT))
    painter.setBrush(gradient)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(0, 0, size, size, radius, radius)

    # 向上箭头
    scale = size / 64.0
    points = [
        QPoint(int(32 * scale), int(14 * scale)),
        QPoint(int(48 * scale), int(33 * scale)),
        QPoint(int(38 * scale), int(33 * scale)),
        QPoint(int(38 * scale), int(50 * scale)),
        QPoint(int(26 * scale), int(50 * scale)),
        QPoint(int(26 * scale), int(33 * scale)),
        QPoint(int(16 * scale), int(33 * scale)),
    ]
    painter.setBrush(QColor("#ffffff"))
    painter.drawPolygon(QPolygon(points))
    painter.end()
    return pixmap


def app_icon() -> QIcon:
    """多尺寸图标，托盘与窗口标题栏都用它。"""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(app_pixmap(size))
    return icon
