"""浅色设计系统（Apple / macOS 浅色语言）。

设计语言要点
------------
* **层次化表面**：窗口底 ``#f5f5f7`` → 面板纯白 → 卡片纯白带阴影，靠明度差分层；
* **大圆角**：卡片 14px、按钮与输入框 8px、角标与动作按钮为胶囊（全圆角）；
* **发丝描边**：``rgba(0,0,0,0.06~0.12)``，绝不用深色硬边框；
* **柔和阴影**：Qt 样式表不支持 box-shadow，卡片阴影在委托里手绘几层低透明度圆角矩形；
* **克制的强调色**：系统蓝 ``#0071e3``，成功绿 ``#34c759``、警告橙 ``#ff9500``、危险红 ``#ff3b30``；
* **排版层次**：标题 semibold、正文常规、次要信息用灰 ``#6e6e73``。

所有颜色/尺寸只在本文件定义，界面各处引用常量，不写死数值。
"""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

# ------------------------------------------------------------------ 颜色
BG = "#f5f5f7"            # 窗口底色
BG_DARK = "#ffffff"       # 面板 / 卡片
BG_DEEP = "#f0f0f2"       # 凹槽 / 输入框
PANEL = "#ffffff"         # 次级按钮底色
BORDER = "#d2d2d7"        # 发丝描边
BORDER_SOFT = "rgba(0, 0, 0, 0.06)"
ACCENT = "#0071e3"        # 系统蓝
ACCENT_DARK = "#0058b0"   # 按下态
ACCENT_TINT = "#e8f1fd"   # 蓝色淡底（胶囊按钮）
TEXT = "#1d1d1f"
TEXT_DIM = "#6e6e73"
TEXT_FAINT = "#8e8e93"
OK = "#34c759"
OK_TINT = "#e4f7e9"
WARN = "#ff9500"
WARN_TINT = "#fff3e0"
DANGER = "#ff3b30"
DANGER_DARK = "#d70015"
DANGER_TINT = "#ffeceb"
HDD_BADGE = "#e8e8ed"     # 机械盘角标：中性灰底
SSD_BADGE = "#d9f5e0"     # 加速中角标：绿底
SEPARATOR = "rgba(0, 0, 0, 0.08)"

# ------------------------------------------------------------------ 尺寸
#: 卡片尺寸（封面保持 2:3，与 Steam 的 600x900 一致）
CARD_W = 208
COVER_H = 312
INFO_H = 88
CARD_H = COVER_H + INFO_H
CARD_RADIUS = 14
CHIP_H = 26
CHIP_GAP = 8
MARGIN = 12
#: 信息区内的纵向位置（相对封面底部）
NAME_TOP = 10
BADGE_TOP = 34
CHIP_TOP = 58
GRID_PAD = 28

STYLE_SHEET = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: -apple-system, "SF Pro Text", "Segoe UI Variable", "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
}}
/* 关键：文字控件绝不能继承底色，否则每个标签后面都会多出一块灰条 */
QLabel, QCheckBox, QRadioButton, QTabBar, QMenuBar {{
    background-color: transparent;
}}
QMainWindow, QDialog {{ background-color: {BG}; }}

QFrame#TopPanel {{
    background-color: {BG_DARK};
    border: 1px solid {BORDER_SOFT};
    border-radius: 16px;
}}
QFrame#TopBar {{
    background-color: transparent;
    border: none;
}}
QFrame#ToolBar {{
    background-color: transparent;
    border: none;
}}
QFrame#HairLine {{
    background-color: {SEPARATOR};
    border: none;
    max-height: 1px;
    min-height: 1px;
}}
QFrame#ProgressPanel {{
    background-color: {BG_DARK};
    border: 1px solid {BORDER_SOFT};
    border-radius: 16px;
}}
QFrame#Segmented {{
    background-color: {BG_DEEP};
    border: 1px solid {BORDER};
    border-radius: 9px;
}}
/* 盘符信息卡：圆角浅灰卡片（母盘 / 加速盘） */
QFrame#StatCard {{
    background-color: {BG_DEEP};
    border: 1px solid {BORDER_SOFT};
    border-radius: 12px;
}}
QPushButton#Segment {{
    background-color: transparent;
    border: none;
    border-radius: 7px;
    padding: 5px 14px;
    color: {TEXT};
}}
QPushButton#Segment:hover {{ background-color: rgba(0, 0, 0, 0.055); }}
QPushButton#Segment:pressed {{ background-color: rgba(0, 0, 0, 0.10); }}
QPushButton#Segment:disabled {{ color: {TEXT_FAINT}; background-color: transparent; }}
QFrame#Card {{
    background-color: {BG_DARK};
    border: 1px solid {BORDER_SOFT};
    border-radius: {CARD_RADIUS}px;
}}
QFrame#TaskRow {{
    background-color: {BG};
    border: 1px solid {BORDER_SOFT};
    border-radius: 10px;
}}

QLabel#Title {{ font-size: 20px; font-weight: 600; color: {TEXT}; letter-spacing: 0.2px; }}
QLabel#SubTitle {{ color: {TEXT_DIM}; font-size: 12px; }}
QLabel#SectionTitle {{ color: {TEXT}; font-size: 14px; font-weight: 600; padding: 2px 0; }}
QLabel#Dim {{ color: {TEXT_DIM}; }}
QLabel#Ok {{ color: {OK}; font-weight: 600; }}
QLabel#Warn {{ color: {WARN}; font-weight: 600; }}
QLabel#Bad {{ color: {DANGER}; font-weight: 600; }}
QLabel#EngineChip {{
    color: {TEXT_DIM};
    background-color: {BG_DEEP};
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
    padding: 1px 8px;
}}

QPushButton {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 14px;
    color: {TEXT};
}}
QPushButton:hover {{ background-color: {BG_DEEP}; }}
QPushButton:pressed {{ background-color: #e3e3e8; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background-color: {BG_DEEP}; border-color: {BORDER_SOFT}; }}
QPushButton#Primary {{
    background-color: {ACCENT}; border: 1px solid {ACCENT}; color: #ffffff; font-weight: 600;
}}
QPushButton#Primary:hover {{ background-color: #1a7fe8; }}
QPushButton#Primary:pressed {{ background-color: {ACCENT_DARK}; }}
QPushButton#Primary:disabled {{
    background-color: {BG_DEEP}; border-color: {BORDER_SOFT}; color: {TEXT_FAINT}; font-weight: 400;
}}
QPushButton#Danger {{ background-color: {DANGER}; border: 1px solid {DANGER}; color: #ffffff; font-weight: 600; }}
QPushButton#Danger:hover {{ background-color: #ff5a50; }}
QPushButton#Danger:pressed {{ background-color: {DANGER_DARK}; }}
QPushButton#Danger:disabled {{
    background-color: {BG_DEEP}; border-color: {BORDER_SOFT}; color: {TEXT_FAINT}; font-weight: 400;
}}
QPushButton#Ghost {{ background-color: transparent; border: 1px solid transparent; color: {ACCENT}; }}
QPushButton#Ghost:hover {{ background-color: {ACCENT_TINT}; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: {BG_DARK};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 10px;
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background-color: {BG_DARK};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
    selection-background-color: {ACCENT_TINT};
    selection-color: {TEXT};
    outline: none;
}}

/* 工具栏里的弹出按钮：只有文字 + 箭头，没有边框和底色（去掉"灰条/阴影"观感） */
QComboBox#ToolbarCombo {{
    background-color: transparent;
    border: none;
    border-radius: 7px;
    padding: 4px 6px;
    color: {TEXT};
}}
QComboBox#ToolbarCombo:hover {{ background-color: rgba(0, 0, 0, 0.055); }}
QComboBox#ToolbarCombo::drop-down {{ border: none; width: 16px; }}

/* 搜索框：无边框浅灰胶囊 */
QLineEdit#SearchField {{
    background-color: {BG_DEEP};
    border: none;
    border-radius: 8px;
    padding: 6px 10px;
    color: {TEXT};
}}
QLineEdit#SearchField:focus {{ background-color: {BG_DARK}; border: 1px solid {ACCENT}; }}

QListView#GameGrid {{
    background-color: transparent;
    border: none;
    padding: 4px;
    outline: none;
}}
QListView#GameGrid::item {{ border: none; }}

QProgressBar {{
    background-color: {BG_DEEP};
    border: none;
    border-radius: 6px;
    height: 12px;
    text-align: center;
    color: {TEXT_DIM};
    font-size: 10px;
}}
QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 6px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #c9c9ce; border-radius: 5px; min-height: 36px; }}
QScrollBar::handle:vertical:hover {{ background: #a8a8ae; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #c9c9ce; border-radius: 5px; min-width: 36px; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 10px; top: -1px; background: {BG_DARK}; }}
QTabBar::tab {{
    background: transparent;
    border: none;
    padding: 8px 18px;
    color: {TEXT_DIM};
    border-radius: 8px;
    margin: 2px;
}}
QTabBar::tab:selected {{ background: {BG_DARK}; color: {TEXT}; font-weight: 600; }}
QTabBar::tab:hover {{ color: {TEXT}; }}

QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {BG_DARK};
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QStatusBar {{ background: {BG_DARK}; color: {TEXT_DIM}; border-top: 1px solid {SEPARATOR}; }}
QToolTip {{
    background-color: {BG_DARK}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 8px;
}}
QMenu {{ background-color: {BG_DARK}; border: 1px solid {BORDER}; border-radius: 10px; padding: 5px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background-color: {ACCENT_TINT}; color: {TEXT}; }}
QListWidget {{
    background-color: {BG_DARK}; border: 1px solid {BORDER}; border-radius: 10px; padding: 4px;
}}
QListWidget::item {{ padding: 5px 6px; border-radius: 6px; }}
QListWidget::item:selected {{ background-color: {ACCENT_TINT}; color: {TEXT}; }}
QDialogButtonBox {{ background: transparent; }}
"""


def apply_theme(app: QApplication) -> None:
    """把浅色主题应用到整个应用。"""
    app.setStyleSheet(STYLE_SHEET)


def qcolor(hex_text: str, alpha: int = 255) -> QColor:
    """十六进制颜色字符串 → QColor（可带透明度）。"""
    color = QColor(hex_text)
    color.setAlpha(alpha)
    return color


def badge_color(status: str) -> tuple[str, str, str]:
    """按状态返回角标 ``(文字, 底色, 文字色)``（浅色底 + 深色字）。"""
    from steam_scanner import (
        ST_ACCELERATED,
        ST_ACCELERATING,
        ST_MISSING,
        ST_ON_HDD,
        ST_ON_SSD,
        ST_UNKNOWN,
        ST_WRITING_BACK,
        STATUS_LABELS,
    )

    mapping = {
        ST_ON_HDD: (HDD_BADGE, "#3a3a3c"),
        ST_ON_SSD: (SSD_BADGE, "#0b7a34"),
        ST_ACCELERATED: (SSD_BADGE, "#0b7a34"),
        ST_ACCELERATING: (WARN_TINT, "#9a5b00"),
        ST_WRITING_BACK: (WARN_TINT, "#9a5b00"),
        ST_MISSING: (DANGER_TINT, DANGER_DARK),
        ST_UNKNOWN: ("#efe6ff", "#5b3bb5"),
    }
    background, foreground = mapping.get(status, (HDD_BADGE, "#3a3a3c"))
    return STATUS_LABELS.get(status, "未知"), background, foreground
