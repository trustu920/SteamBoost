"""主窗口：顶栏（盘符空间/搜索/排序）+ 筛选与批量操作 + 虚拟化游戏网格。

本文件只负责"看得见的壳"和用户意图的转发：
真正的重活（扫描、复制、删除）由 ``workers`` 里的线程执行，
删除确认一律走 :mod:`deletion_guard` 的闸门（见 :mod:`ui.confirm_dialog`）。
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPropertyAnimation, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMenu,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from config import Config, human_size, volume_free_bytes, volume_total_bytes
from steam_scanner import (
    ACCELERATABLE,
    RELEASABLE,
    ST_ACCELERATED,
    ST_ON_HDD,
    GameRecord,
    ScanReport,
)
from ui import theme
from ui.covers import CoverCache
from ui.game_model import GameCardDelegate, GameListModel, GameRole, chip_rects, actions_for
from ui.icons import app_pixmap
from ui.progress_panel import ProgressPanel

SORT_OPTIONS = [
    ("recent", "最近游玩"),
    ("size", "按大小"),
    ("name", "按名称"),
]
FILTER_OPTIONS = [
    ("all", "全部"),
    ("acceleratable", "可加速"),
    ("accelerated", "已加速"),
]


class GameGridView(QListView):
    """游戏网格视图：按像素滚动 + 滚轮短动画，避免"一格跳一整行"的生硬感。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self._animation = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._animation.setDuration(170)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def wheelEvent(self, event) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        bar = self.verticalScrollBar()
        # 一格滚轮 ≈ 1/3 行卡片高度，滚动更细腻
        step = max(48, self.gridSize().height() // 3)
        target = bar.value() - int(delta / 120 * step)
        target = max(bar.minimum(), min(bar.maximum(), target))
        self._animation.stop()
        self._animation.setStartValue(bar.value())
        self._animation.setEndValue(target)
        self._animation.start()
        event.accept()


class DriveSpaceBar(QFrame):
    """一个盘符的信息卡：圆角浅灰卡片 + 两行标签 + 胶囊占用条。

    标签**故意拆成两行**（"母盘（母本） F:" / "可用 285.00 GiB / 1.00 TB"）：
    写成一行时这段文字比卡片宽，会被裁成"可用 285.00 Gi"。
    """

    def __init__(self, role_text: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("StatCard")
        self.role_text = role_text
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setSpacing(3)
        self.title = QLabel(f"{role_text}：未选择")
        self.title.setObjectName("Dim")
        self.detail = QLabel("")
        self.detail.setObjectName("Dim")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)          # 细条 + 全圆角 = 胶囊
        self.bar.setRange(0, 100)
        layout.addWidget(self.title)
        layout.addWidget(self.detail)
        layout.addWidget(self.bar)
        self.setMinimumWidth(190)

    def update_drive(self, drive: str, role_text: str) -> None:
        # 卡片内底色是浅灰，轨道用白色对比更清楚；两端圆角与高度一半一致
        track = "QProgressBar { background-color: #ffffff; border: none; border-radius: 3px; }"
        if not drive:
            self.title.setText(f"{role_text}：未选择")
            self.detail.setText("")
            self.bar.setValue(0)
            self.bar.setStyleSheet(track)
            return
        total = volume_total_bytes(drive)
        free = volume_free_bytes(drive)
        if total <= 0:
            self.title.setText(f"{role_text}：{drive}: 不可用")
            self.detail.setText("")
            return
        used_percent = int(round((total - free) / total * 100))
        self.bar.setValue(max(0, min(100, used_percent)))
        self.title.setText(f"{role_text} {drive}:")
        self.detail.setText(f"可用 {human_size(free)} / {human_size(total)}")
        color = theme.ACCENT if used_percent < 85 else theme.DANGER
        self.bar.setStyleSheet(track + f"QProgressBar::chunk {{ background-color: {color}; border-radius: 3px; }}")


class MainWindow(QMainWindow):
    """主窗口。对外暴露的信号让控制层接上真正的操作。"""

    scanRequested = Signal()
    operationRequested = Signal(str, list)   # (action, [appid, ...])
    settingsRequested = Signal()
    quarantineRequested = Signal()
    repairRequested = Signal()

    def __init__(self, cfg: Config | None = None, allow_cover_download: bool = True) -> None:
        super().__init__()
        self.cfg = cfg or Config.load()
        self.report: ScanReport | None = None
        self.cover_cache = CoverCache(self, allow_download=allow_cover_download)
        self.cover_cache.coverReady.connect(self._on_cover_ready)

        self.setWindowTitle("SteamBoost — Steam 游戏 SSD 加速缓存")
        self.resize(1180, 820)
        #: 由 main.py 在托盘可用时置为 True
        self.tray_available = False
        self._build_ui()
        self._update_drive_bars()

    def closeEvent(self, event) -> None:  # noqa: N802
        """按设置决定"关闭即退出"还是"最小化到托盘"。"""
        if self.cfg.minimize_to_tray and self.tray_available:
            event.ignore()
            self.hide()
            self.set_status("已最小化到系统托盘（双击托盘图标可恢复）")
            return
        event.accept()

    # ------------------------------------------------------------ 界面搭建
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        # 留出外边距：圆角面板的圆角与阴影才看得出来
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(10)

        root.addWidget(self._build_top_panel())
        root.addWidget(self._build_grid(), 1)
        self.progress_panel = self._build_progress_panel()
        root.addWidget(self.progress_panel)

        status = QStatusBar()
        self.status_label = QLabel("准备就绪")
        status.addWidget(self.status_label)
        #: 实际生效的复制引擎（由控制器刷新）——避免"选了 FastCopy 却在跑 robocopy"
        self.engine_label = QLabel("")
        self.engine_label.setObjectName("EngineChip")
        status.addPermanentWidget(self.engine_label)
        self.anomaly_label = QLabel("")
        status.addPermanentWidget(self.anomaly_label)
        self.setStatusBar(status)

    def _build_top_panel(self) -> QWidget:
        """顶栏合并为**一块圆角面板**：上半是标题/盘符/操作，下半是搜索与筛选。"""
        panel = QFrame()
        panel.setObjectName("TopPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(0)
        layout.addWidget(self._build_top_bar())

        hairline = QFrame()
        hairline.setObjectName("HairLine")
        layout.addSpacing(10)
        layout.addWidget(hairline)
        layout.addSpacing(10)
        layout.addWidget(self._build_toolbar())

        shadow = QGraphicsDropShadowEffect(panel)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 26))
        panel.setGraphicsEffect(shadow)
        return panel

    def _build_action_segments(self) -> QWidget:
        """右上角三个操作合成一个分段控件（Apple 的 Segmented Control）。"""
        frame = QFrame()
        frame.setObjectName("Segmented")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(1)

        self.refresh_button = QPushButton("重新扫描")
        self.refresh_button.setObjectName("Segment")
        self.refresh_button.clicked.connect(self.scanRequested.emit)
        self.quarantine_button = QPushButton("隔离区")
        self.quarantine_button.setObjectName("Segment")
        self.quarantine_button.clicked.connect(self.quarantineRequested.emit)
        self.repair_button = QPushButton("修复向导")
        self.repair_button.setObjectName("Segment")
        self.repair_button.clicked.connect(self.repairRequested.emit)
        self.settings_button = QPushButton("设置")
        self.settings_button.setObjectName("Segment")
        self.settings_button.clicked.connect(self.settingsRequested.emit)

        for button in (
            self.refresh_button,
            self.quarantine_button,
            self.repair_button,
            self.settings_button,
        ):
            layout.addWidget(button)
        return frame

    def _build_top_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        # 左上角：圆角应用图标 + 标题（图标给这块区域一个克制的圆角元素）
        icon_label = QLabel()
        icon_label.setPixmap(app_pixmap(34))
        icon_label.setFixedSize(34, 34)

        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel("SteamBoost")
        title.setObjectName("Title")
        subtitle = QLabel("把机械盘上的游戏加速到固态盘，玩完可随时回写或释放")
        subtitle.setObjectName("SubTitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)

        left_box = QHBoxLayout()
        left_box.setSpacing(10)
        left_box.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignVCenter)
        left_box.addLayout(title_box)
        layout.addLayout(left_box)
        layout.addStretch(1)

        self.mother_bar = DriveSpaceBar("母盘（母本）")
        self.cache_bar = DriveSpaceBar("加速盘（缓存）")
        layout.addWidget(self.mother_bar)
        layout.addWidget(self.cache_bar)
        layout.addWidget(self._build_action_segments())
        return bar

    def _build_toolbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        self.search = QLineEdit()
        self.search.setObjectName("SearchField")
        self.search.setPlaceholderText("搜索游戏名称或 appid…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(300)
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)

        self.sort_combo = QComboBox()
        self.sort_combo.setObjectName("ToolbarCombo")
        for _, label in SORT_OPTIONS:
            self.sort_combo.addItem(label)
        self.sort_combo.currentIndexChanged.connect(self._apply_filter)
        layout.addWidget(QLabel("排序"))
        layout.addWidget(self.sort_combo)

        self.filter_combo = QComboBox()
        self.filter_combo.setObjectName("ToolbarCombo")
        for _, label in FILTER_OPTIONS:
            self.filter_combo.addItem(label)
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)
        layout.addWidget(QLabel("筛选"))
        layout.addWidget(self.filter_combo)

        layout.addStretch(1)

        self.selection_label = QLabel("未选中")
        self.selection_label.setObjectName("Dim")
        layout.addWidget(self.selection_label)

        # 原来的三个批量按钮收成一个下拉：省地方，也不会出现一排灰按钮
        self.batch_button = QPushButton("批量操作 ▾")
        self.batch_button.setObjectName("Primary")
        menu = QMenu(self.batch_button)
        for key, label in (
            ("accelerate", "加速选中"),
            ("writeback", "回写选中"),
            ("release", "释放选中"),
        ):
            action = menu.addAction(label)
            action.triggered.connect(lambda _checked=False, k=key: self._batch(k))
        self.batch_button.setMenu(menu)
        self.batch_button.setEnabled(False)
        layout.addWidget(self.batch_button)
        return bar

    def _build_grid(self) -> QWidget:
        self.model = GameListModel(self.cover_cache, self)
        self.view = GameGridView()
        self.view.setObjectName("GameGrid")
        self.view.setModel(self.model)
        self.view.setItemDelegate(GameCardDelegate(self.view))
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setUniformItemSizes(True)
        self.view.setLayoutMode(QListView.LayoutMode.Batched)   # 大列表滚动更顺
        self.view.setBatchSize(60)
        self.view.setGridSize(QSize(theme.CARD_W + 16, theme.CARD_H + 16))
        self.view.setSpacing(4)
        self.view.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.selectionModel().selectionChanged.connect(self._on_selection_changed)
        # 关键：鼠标事件发给的是 viewport，不是 QListView 本身，
        # 过滤器必须装在 viewport 上，否则卡片上的按钮永远收不到点击。
        self.view.viewport().installEventFilter(self)
        self.view.viewport().setMouseTracking(True)
        return self.view

    def _build_progress_panel(self) -> QWidget:
        """进度面板：每个任务一行（进度条 / 速度 / ETA / 取消），并展示排队情况。"""
        self.progress = ProgressPanel()
        return self.progress

    # ------------------------------------------------------------ 事件处理
    def eventFilter(self, obj, event):  # noqa: N802
        """在网格里点击卡片上的动作按钮（按钮是画出来的，需要命中判断）。

        注意对象是 **viewport**：Qt 把鼠标事件发给滚动区域的视口子控件，
        装在 QListView 本身上的过滤器收不到这些事件。
        """
        if obj is self.view.viewport() and event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                pos = event.position().toPoint()
                index = self.view.indexAt(pos)
                if index.isValid():
                    game: GameRecord = index.data(GameRole)
                    if game is not None:
                        actions = actions_for(game)
                        for rect, (key, _label) in zip(
                            chip_rects(self.view.visualRect(index), len(actions)), actions
                        ):
                            if rect.contains(pos):
                                # 命中按钮：吞掉事件，交给业务层，不改变选中状态
                                self.operationRequested.emit(key, [game.appid])
                                return True
        return super().eventFilter(obj, event)

    def chip_rect_at(self, pos: QPoint) -> tuple[str, str] | None:
        """给定视口坐标，返回命中的 ``(动作, appid)``；没命中返回 None。

        与绘制共用 :func:`chip_rects`，保证"看到的位置"就是"点的位置"。
        """
        index = self.view.indexAt(pos)
        if not index.isValid():
            return None
        game: GameRecord = index.data(GameRole)
        if game is None:
            return None
        actions = actions_for(game)
        for rect, (key, _label) in zip(chip_rects(self.view.visualRect(index), len(actions)), actions):
            if rect.contains(pos):
                return key, game.appid
        return None

    def _on_selection_changed(self) -> None:
        appids = self.selected_appids()
        self.selection_label.setText(f"已选中 {len(appids)} 项" if appids else "未选中")
        self.batch_button.setEnabled(bool(appids))

    def _batch(self, action: str) -> None:
        appids = self.selected_appids()
        if appids:
            self.operationRequested.emit(action, appids)

    def selected_appids(self) -> list[str]:
        return [index.data(GameRole).appid for index in self.view.selectionModel().selectedIndexes()]

    def _batch_action(self, action: str) -> None:
        """按动作类型批量执行（供外部调用）。"""
        self._batch(action)

    # ------------------------------------------------------------ 数据装载
    def load_report(self, report: ScanReport) -> None:
        """把扫描结果装进界面。"""
        self.report = report
        self._apply_filter()
        self._update_drive_bars()

        parts = [f"共 {len(report.games)} 款游戏", f"库 {len(report.libraries)} 个"]
        counts: dict[str, int] = {}
        for game in report.games:
            counts[game.status_label] = counts.get(game.status_label, 0) + 1
        parts.extend(f"{label} {count}" for label, count in counts.items())
        self.status_label.setText("　|　".join(parts))

        errors = [a for a in report.anomalies if a.level == "error"]
        warnings = [a for a in report.anomalies if a.level != "error"]
        if errors or warnings or report.quarantine_items:
            self.anomaly_label.setText(
                f"自检：错误 {len(errors)}　提醒 {len(warnings)}　隔离区 {len(report.quarantine_items)}"
            )
        else:
            self.anomaly_label.setText("自检：无异常")

    def _update_drive_bars(self) -> None:
        self.mother_bar.update_drive(self.cfg.mother_drive, "母盘（母本）")
        self.cache_bar.update_drive(self.cfg.cache_drive, "加速盘（缓存）")

    def _apply_filter(self) -> None:
        if self.report is None:
            return
        keyword = self.search.text().strip().lower()
        mode = FILTER_OPTIONS[self.filter_combo.currentIndex()][0]
        sort_key = SORT_OPTIONS[self.sort_combo.currentIndex()][0]

        games = []
        for game in self.report.games:
            if keyword and keyword not in game.name.lower() and keyword not in game.appid:
                continue
            if mode == "acceleratable" and game.status not in ACCELERATABLE:
                continue
            if mode == "accelerated" and game.status not in RELEASABLE:
                continue
            games.append(game)

        if sort_key == "recent":
            games.sort(key=lambda g: (-(g.last_played or 0), g.name.lower()))
        elif sort_key == "size":
            games.sort(key=lambda g: (-(g.size_on_disk or 0), g.name.lower()))
        else:
            games.sort(key=lambda g: g.name.lower())

        self.model.set_games(games)
        self._on_selection_changed()

    def _on_cover_ready(self, appid: str) -> None:
        self.model.refresh_cover(appid)

    # ------------------------------------------------------------ 进度显示
    def show_progress(self, text: str, percent: float = 0, cancel_enabled: bool = True) -> None:
        """更新任务队列标题（逐任务的进度由 ProgressPanel 自己维护）。"""
        self.progress.title.setText(text)

    def clear_progress(self, text: str = "任务队列：空闲") -> None:
        self.progress.title.setText(text)

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def set_engine_text(self, text: str) -> None:
        """状态栏右侧显示当前实际使用的复制引擎。"""
        self.engine_label.setText(f"引擎：{text}" if text else "")
        # 回退到 robocopy 时用警示色，正常时用弱化色
        self.engine_label.setObjectName("Warn" if "回退" in text else "EngineChip")
        self.engine_label.style().unpolish(self.engine_label)
        self.engine_label.style().polish(self.engine_label)
