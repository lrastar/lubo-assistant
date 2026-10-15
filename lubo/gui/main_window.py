"""现代化主窗口：录播分段管理、资源配对、高级参数配置、实时运行控制与日志。

提供现代卡片式视觉风格、深浅主题切换、工作流步骤导航与全功能录播流水线控制。
"""
from __future__ import annotations

import os
import subprocess
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent, QColor, QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox,
    QFileDialog, QFontComboBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSpinBox, QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from lubo.config import Settings, load_settings, save_settings, settings_path
from lubo.asr_profiles import ASR_PROFILES, get_asr_profile
from lubo.ffmpeg import FfmpegError, MediaInfo, find_tool, probe
from lubo.segments import (
    VIDEO_SUFFIXES, Segment, default_output_base, is_source_video, make_segment, sort_segments,
)
from lubo.gui.theme import ThemeManager, fade_transition, icon_text
from lubo.gui.job_controller import JobController

APP_TITLE = "录播助手"
APP_SUBTITLE = "LuboStudio · 录播分段拼接 · 弹幕烧录 · 自动字幕识别"
SEGMENT_ROLE = Qt.ItemDataRole.UserRole
TOKEN_ROLE = Qt.ItemDataRole.UserRole + 1
LOG_MAX_LINES = 5000
COLUMNS = ("序号", "文件名", "开始时间", "时长", "分辨率", "弹幕", "封面", "大小")
COL_DURATION, COL_RESOLUTION = 3, 4
INVALID_NAME_CHARS = set('<>:"/\\|?*')
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
RESOURCE_LABELS = {"video": "视频", "xml": "弹幕 XML", "cover": "封面图片"}

ENCODERS = [
    ("h264_nvenc", "H.264（NVENC 显卡硬件加速）"),
    ("hevc_nvenc", "HEVC（NVENC 显卡硬件加速）"),
    ("libx264", "H.264（x264 CPU 软解编码）"),
]
NVENC_PRESETS = [
    (f"p{i}", f"p{i}" + {1: "（最快速度）", 5: "（平衡推荐）", 7: "（最高质量）"}.get(i, ""))
    for i in range(1, 8)
]
X264_PRESETS = [
    (p, p) for p in (
        "ultrafast", "superfast", "veryfast", "faster", "fast",
        "medium", "slow", "slower", "veryslow"
    )
]
AUDIO_BITRATES = [(b, b) for b in ("128k", "160k", "192k", "256k", "320k")]
ASR_DEVICES = [("cuda", "显卡（CUDA 加速）"), ("cpu", "中央处理器（CPU）")]
ASR_ENGINES = [
    ("quality", "高精度模式（本地大模型，文字与时间轴更准）"),
    ("paraformer", "快捷模式（Paraformer 实时方案）"),
]
ASR_LANGUAGES = [
    ("", "自动检测语言"),
    ("Chinese", "中文（普通话）"),
    ("English", "英语 (English)"),
    ("Japanese", "日语 (日本語)"),
    ("Cantonese", "粤语"),
]
QUALITY_MODELS = [(profile.key, profile.display_name) for profile in ASR_PROFILES.values()]


# ---------------------------------------------------------------- 文本与数值格式化

def fmt_duration(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 3600:d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def fmt_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


# ---------------------------------------------------------------- 异步扫描与探测

class _TaskSignals(QObject):
    scanned = Signal(object, str)     # (list[Segment], list[Path]), 错误信息
    probed = Signal(int, object, str) # token, MediaInfo | None, 错误信息


def resource_kind(path: Path) -> str | None:
    if path.suffix.lower() in VIDEO_SUFFIXES:
        return "video"
    if path.suffix.lower() == ".xml":
        return "xml"
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return "cover"
    return None


def expand_resource_paths(paths: list[Path]) -> list[Path]:
    """将输入的路径展开为有效资源列表，智能区分原片与中间文件。"""
    resources: list[Path] = []
    for path in paths:
        if path.is_dir():
            entries = sorted(p for p in path.iterdir() if p.is_file() and resource_kind(p))
            if any(is_source_video(p) for p in entries):
                entries = [p for p in entries if resource_kind(p) != "video" or is_source_video(p)]
            resources.extend(entries)
        elif path.is_file() and resource_kind(path):
            resources.append(path)
    return resources


class _ScanTask(QRunnable):
    def __init__(self, paths: list[Path], signals: _TaskSignals) -> None:
        super().__init__()
        self._paths = paths
        self._signals = signals

    def run(self) -> None:
        try:
            resources = expand_resource_paths(self._paths)
            segments = [make_segment(p, probe_media=False)
                        for p in resources if resource_kind(p) == "video"]
            self._signals.scanned.emit((sort_segments(segments), resources), "")
        except Exception as exc:  # noqa: BLE001
            self._signals.scanned.emit(([], []), f"扫描资源失败：{exc}")


class _ProbeTask(QRunnable):
    def __init__(self, token: int, video: Path, signals: _TaskSignals) -> None:
        super().__init__()
        self._token = token
        self._video = video
        self._signals = signals

    def run(self) -> None:
        try:
            self._signals.probed.emit(self._token, probe(self._video), "")
        except Exception as exc:  # noqa: BLE001
            self._signals.probed.emit(self._token, None, str(exc) or type(exc).__name__)


# ---------------------------------------------------------------- 现代控件

class SegmentTree(QTreeWidget):
    """录播分段列表树：支持内部平滑拖拽排序、系统文件拖入。"""

    filesDropped = Signal(list)
    orderChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setColumnCount(len(COLUMNS))
        self.setHeaderLabels(list(COLUMNS))
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)
        self.setAcceptDrops(True)
        header = self.header()
        header.setStretchLastSection(False)
        for col in range(len(COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

    def _is_external(self, event) -> bool:
        return event.source() is not self and event.mimeData().hasUrls()

    def dragEnterEvent(self, event) -> None:
        if self._is_external(event):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if self._is_external(event):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if self._is_external(event):
            paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
            event.acceptProposedAction()
            if paths:
                self.filesDropped.emit(paths)
            return
        super().dropEvent(event)
        self.orderChanged.emit()


class ResourceTree(QTreeWidget):
    """侧车资源列表树（弹幕 XML 与封面图片）。"""

    filesDropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setColumnCount(3)
        self.setHeaderLabels(["类型", "文件名", "关联视频状态"])
        self.setRootIsDecorated(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.header().resizeSection(2, 180)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.filesDropped.emit(paths)
        else:
            super().dropEvent(event)


class ColorButton(QPushButton):
    """现代取色器按钮：带色块显示与十六进制颜色值。"""

    def __init__(self, color: str = "#FFFFFF", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor(color)
        self.clicked.connect(self._choose)
        self._refresh()

    def color(self) -> str:
        return self._color.name().upper()

    def setColor(self, color: str) -> None:
        parsed = QColor(color)
        if parsed.isValid():
            self._color = parsed
            self._refresh()

    def _choose(self) -> None:
        chosen = QColorDialog.getColor(self._color, self, "选择颜色")
        if chosen.isValid():
            self._color = chosen
            self._refresh()

    def _refresh(self) -> None:
        text_color = "#000000" if self._color.lightness() > 140 else "#FFFFFF"
        self.setText(f"  {self.color()}  ")
        self.setStyleSheet(
            f"background-color: {self.color()}; color: {text_color}; "
            f"border: 1px solid #718096; border-radius: 6px; font-weight: bold; min-height: 22px;"
        )


@dataclass
class _Binding:
    section: str
    field: str
    widget: QWidget


def _get_widget_value(widget: QWidget):
    if isinstance(widget, QCheckBox):
        return widget.isChecked()
    if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
        return widget.value()
    if isinstance(widget, QFontComboBox):
        return widget.currentFont().family()
    if isinstance(widget, QComboBox):
        return widget.currentData()
    if isinstance(widget, QLineEdit):
        return widget.text().strip()
    if isinstance(widget, ColorButton):
        return widget.color()
    raise TypeError(type(widget))


def _set_widget_value(widget: QWidget, value) -> None:
    if isinstance(widget, QCheckBox):
        widget.setChecked(bool(value))
    elif isinstance(widget, QSpinBox):
        widget.setValue(int(value))
    elif isinstance(widget, QDoubleSpinBox):
        widget.setValue(float(value))
    elif isinstance(widget, QFontComboBox):
        widget.setCurrentFont(QFont(str(value)))
    elif isinstance(widget, QComboBox):
        _combo_select(widget, value)
    elif isinstance(widget, QLineEdit):
        widget.setText(str(value))
    elif isinstance(widget, ColorButton):
        widget.setColor(str(value))
    else:
        raise TypeError(type(widget))


def _combo(items: list[tuple[str, str]]) -> QComboBox:
    box = QComboBox()
    for value, label in items:
        box.addItem(label, value)
    return box


def _combo_select(box: QComboBox, value) -> None:
    index = box.findData(value)
    if index < 0 and value not in (None, ""):
        box.addItem(str(value), value)
        index = box.count() - 1
    if index >= 0:
        box.setCurrentIndex(index)


def _spin(lo: int, hi: int, suffix: str = "") -> QSpinBox:
    box = QSpinBox()
    box.setRange(lo, hi)
    box.setSuffix(suffix)
    return box


def _dspin(lo: float, hi: float, step: float, decimals: int, suffix: str = "") -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(lo, hi)
    box.setSingleStep(step)
    box.setDecimals(decimals)
    box.setSuffix(suffix)
    return box


def _form_page(rows: list[tuple[str, QWidget]]) -> QWidget:
    page = QWidget()
    form = QFormLayout(page)
    form.setContentsMargins(12, 12, 12, 12)
    form.setVerticalSpacing(10)
    form.setHorizontalSpacing(16)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    for label, widget in rows:
        if label:
            lbl = QLabel(label)
            lbl.setStyleSheet("font-weight: 500;")
            form.addRow(lbl, widget)
        else:
            form.addRow(widget)
    return page


# ---------------------------------------------------------------- 主窗口

class MainWindow(QMainWindow):
    def __init__(self, settings_file: Path | None = None,
                 job_controller: JobController | None = None) -> None:
        super().__init__()
        self._settings_file = settings_file
        self._settings: Settings = load_settings(settings_file)
        self._bindings: list[_Binding] = []
        self._items: dict[int, QTreeWidgetItem] = {}
        self._resources: dict[str, QTreeWidgetItem] = {}
        self._next_token = 1
        self._pending_probes: set[int] = set()
        self._pending_scans = 0
        self._prefix_user_edited = False
        self._restore_order: list[str] | None = None
        self._job_controller = job_controller or JobController(self)
        self._close_pending = False
        self._last_output_dir: Path | None = None
        self._last_srt_path: Path | None = None
        self._current_mode: str | None = None
        self._job_controller.log.connect(self.append_log)
        self._job_controller.step.connect(lambda text: self.step_label.setText(f"⏳ {text}")
                                          if hasattr(self, "step_label") else None)
        self._job_controller.progress.connect(self._on_progress)
        self._job_controller.finished.connect(self._on_finished)
        self._job_controller.failed.connect(self._on_failed)
        self._job_controller.cancelled.connect(self._on_cancelled)
        self._job_controller.runningChanged.connect(self._on_job_running_changed)

        app = QApplication.instance()
        self._theme_mgr = ThemeManager(app, initial_theme=getattr(self._settings, "theme", "dark"))
        self._theme_mgr.themeChanged.connect(self._on_theme_changed)

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)
        self._signals = _TaskSignals(self)
        self._signals.scanned.connect(self._on_scanned)
        self._signals.probed.connect(self._on_probed)

        self.setWindowTitle(APP_TITLE)
        self.resize(1380, 860)
        self.setMinimumSize(1100, 700)

        self._build_ui()
        self._apply_settings()
        for warning in self._settings.load_warnings:
            self.append_log(f"设置校验：{warning}")
        if self._settings.last_output_base:
            self.prefix_edit.setText(self._settings.last_output_base)
            self._prefix_user_edited = self._settings.prefix_user_edited
        self._restore_geometry()
        self._update_summary()
        self._update_workflow_steps()

        restored = self._settings.last_resources or self._settings.last_segments
        if restored:
            self._restore_order = list(self._settings.last_segments)
            self.add_paths([Path(p) for p in restored])

    # ------------------------------------------------------------ 界面构建

    def _build_ui(self) -> None:
        central = QWidget()
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(12, 10, 12, 10)
        main_layout.setSpacing(8)

        # 1. 顶部 Header 与 流程导航
        main_layout.addWidget(self._build_header())

        # 2. 中部主体 Splitter
        main_splitter = QSplitter(Qt.Orientation.Vertical)
        main_splitter.addWidget(self._build_workspace_box())

        lower_splitter = QSplitter(Qt.Orientation.Horizontal)
        lower_splitter.addWidget(self._build_settings_tabs())
        lower_splitter.addWidget(self._build_log_box())
        lower_splitter.setStretchFactor(0, 5)
        lower_splitter.setStretchFactor(1, 5)

        main_splitter.addWidget(lower_splitter)
        main_splitter.setStretchFactor(0, 5)
        main_splitter.setStretchFactor(1, 4)
        main_layout.addWidget(main_splitter, 1)

        # 3. 底部执行控制面板
        main_layout.addWidget(self._build_run_bar())

        self.setCentralWidget(central)

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("AppHeader")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(12)

        # 品牌徽章与标题
        brand_icon = QLabel("录")
        brand_icon.setObjectName("HeaderLogo")
        brand_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(brand_icon)

        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        self.app_title_lbl = QLabel(APP_TITLE)
        self.app_title_lbl.setObjectName("HeaderTitle")
        self.app_subtitle_lbl = QLabel(APP_SUBTITLE)
        self.app_subtitle_lbl.setObjectName("HeaderSubtitle")
        title_box.addWidget(self.app_title_lbl)
        title_box.addWidget(self.app_subtitle_lbl)
        layout.addLayout(title_box)

        layout.addStretch(1)

        # 4步工作流向导
        self.step_bar = QFrame()
        self.step_bar.setObjectName("StepBar")
        step_layout = QHBoxLayout(self.step_bar)
        step_layout.setContentsMargins(4, 2, 4, 2)
        step_layout.setSpacing(4)

        self.step_pills = [
            QLabel("① 导入分段"),
            QLabel("② 生成字幕"),
            QLabel("③ 校对字幕"),
            QLabel("④ 烤入成片"),
        ]
        for i, pill in enumerate(self.step_pills):
            pill.setProperty("class", "step-pill")
            pill.setObjectName(f"StepPill_{i}")
            step_layout.addWidget(pill)
            if i < len(self.step_pills) - 1:
                arrow = QLabel("→")
                arrow.setStyleSheet("color: #64748b; font-size: 11px;")
                step_layout.addWidget(arrow)

        layout.addWidget(self.step_bar)
        layout.addStretch(1)

        # 主题切换按钮
        self.btn_theme = QPushButton()
        self.btn_theme.setObjectName("ThemeToggleBtn")
        self._update_theme_btn_text()
        self.btn_theme.clicked.connect(self._toggle_theme)
        layout.addWidget(self.btn_theme)

        return header

    def _update_theme_btn_text(self) -> None:
        is_dark = self._theme_mgr.current_theme() == "dark"
        self.btn_theme.setText("浅色模式" if is_dark else "深色模式")

    def _toggle_theme(self) -> None:
        self._theme_mgr.toggle_theme()
        self._settings.theme = self._theme_mgr.current_theme()
        fade_transition(self, duration=220)
        self._update_theme_btn_text()

    def _on_theme_changed(self, theme_name: str) -> None:
        self._update_theme_btn_text()

    def _build_workspace_box(self) -> QGroupBox:
        box = QGroupBox("工作区 · 录播分段与资源管理")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 14, 10, 8)
        layout.setSpacing(8)

        # 工具栏
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self.btn_add_folder = QPushButton("添加文件夹")
        self.btn_add_files = QPushButton("添加视频/XML/图片")
        self.btn_up = QPushButton("上移")
        self.btn_down = QPushButton("下移")
        self.btn_remove = QPushButton("移除")
        self.btn_clear = QPushButton("清空")
        self.btn_sort = QPushButton("按时间排序")

        for button in (self.btn_add_folder, self.btn_add_files, self.btn_up,
                       self.btn_down, self.btn_remove, self.btn_clear, self.btn_sort):
            toolbar.addWidget(button)

        toolbar.addStretch(1)

        # 统计 Badge
        self.summary_label = QLabel()
        self.summary_label.setObjectName("SummaryBadge")
        toolbar.addWidget(self.summary_label)

        layout.addLayout(toolbar)

        # 分段与资源选项卡
        self.workspace_tabs = QTabWidget()
        self.workspace_tabs.addTab(self._build_order_pane(), "视频分段")
        self.workspace_tabs.addTab(self._build_resource_pane(), "导入的资源（XML / 图片）")
        layout.addWidget(self.workspace_tabs, 1)

        self._segment_buttons = [
            self.btn_add_folder, self.btn_add_files, self.btn_up, self.btn_down,
            self.btn_remove, self.btn_clear, self.btn_sort, self.btn_add_to_order,
            self.btn_link_resource, self.btn_unlink_resource, self.btn_remove_resource,
        ]

        self.btn_add_folder.clicked.connect(self._choose_folder)
        self.btn_add_files.clicked.connect(self._choose_files)
        self.btn_add_to_order.clicked.connect(self._add_selected_videos_to_order)
        self.btn_link_resource.clicked.connect(self._link_selected_resources)
        self.btn_unlink_resource.clicked.connect(self._unlink_selected_resources)
        self.btn_remove_resource.clicked.connect(self._remove_selected_resources)
        self.btn_up.clicked.connect(lambda: self._move_selected(-1))
        self.btn_down.clicked.connect(lambda: self._move_selected(1))
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_clear.clicked.connect(self.clear_segments)
        self.btn_sort.clicked.connect(self.sort_by_time)

        return box

    def _build_order_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(4, 6, 4, 4)
        self.tree = SegmentTree()
        layout.addWidget(self.tree)
        self.tree.filesDropped.connect(self.add_paths)
        self.tree.orderChanged.connect(self._on_segments_changed)
        self.tree.itemSelectionChanged.connect(self._sync_target_to_selection)

        remove_action = QAction(self.tree)
        remove_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Delete))
        remove_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        remove_action.triggered.connect(self.remove_selected)
        self.tree.addAction(remove_action)
        return pane

    def _build_resource_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(4, 6, 4, 4)
        layout.setSpacing(6)

        actions = QHBoxLayout()
        actions.setSpacing(8)

        self.btn_add_to_order = QPushButton("选中视频加入排序")
        self.resource_target = QComboBox()
        self.resource_target.setMinimumWidth(260)
        self.btn_link_resource = QPushButton("关联到目标视频")
        self.btn_unlink_resource = QPushButton("取消关联")
        self.btn_remove_resource = QPushButton("移除资源")

        actions.addWidget(self.btn_add_to_order)
        actions.addSpacing(8)
        actions.addWidget(QLabel("目标视频:"))
        actions.addWidget(self.resource_target, 1)
        actions.addWidget(self.btn_link_resource)
        actions.addWidget(self.btn_unlink_resource)
        actions.addWidget(self.btn_remove_resource)
        layout.addLayout(actions)

        self.resource_tree = ResourceTree()
        layout.addWidget(self.resource_tree)

        hint = QLabel("💡 提示：同名 XML 弹幕及封面图片会自动配对；若名称不同，可在此选中资源并在上方选择目标视频后点击“关联到目标视频”。")
        hint.setStyleSheet("color: #94a3b8; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.resource_tree.filesDropped.connect(self.add_paths)
        return pane

    def _bind(self, section: str, field: str, widget: QWidget) -> QWidget:
        self._bindings.append(_Binding(section, field, widget))
        return widget

    def _build_settings_tabs(self) -> QTabWidget:
        tabs = QTabWidget()
        self.tabs = tabs
        b = self._bind

        # 1. 输出设置
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText("留空 = 第一个分段所在文件夹")
        self.output_dir_edit.textChanged.connect(self._update_output_hint)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._choose_output_dir)

        dir_row = QWidget()
        dir_layout = QHBoxLayout(dir_row)
        dir_layout.setContentsMargins(0, 0, 0, 0)
        dir_layout.setSpacing(6)
        dir_layout.addWidget(self.output_dir_edit, 1)
        dir_layout.addWidget(browse)

        self.prefix_edit = QLineEdit()
        self.prefix_edit.setPlaceholderText("默认 = 第一个分段的文件名")
        self.prefix_edit.textEdited.connect(self._on_prefix_edited)
        self.prefix_edit.textChanged.connect(self._update_output_hint)

        self.output_hint = QLabel()
        self.output_hint.setWordWrap(True)
        self.output_hint.setStyleSheet("color: #38bdf8; font-size: 11px;")

        b("", "output_dir", self.output_dir_edit)
        tabs.addTab(_form_page([
            ("输出目标文件夹", dir_row),
            ("产物文件名前缀", self.prefix_edit),
            ("", self.output_hint),
            ("", b("", "keep_intermediate", QCheckBox("保留中间文件（烧录分段、合并临时视频、识别 wav 等）"))),
            ("", b("", "overwrite", QCheckBox("覆盖已有文件（不勾选则智能复用已存在的产物，便于断点续跑）"))),
        ]), "输出")

        # 2. 步骤设置
        self.btn_custom = QPushButton("按勾选步骤自定义运行（高级重跑模式）")
        self.btn_custom.clicked.connect(self.start)
        tabs.addTab(_form_page([
            ("", b("steps", "burn_danmaku", QCheckBox("1. 每段烧录弹幕 (XML → NVENC/x264)"))),
            ("", b("steps", "concat", QCheckBox("2. 顺序拼接所有分段 (无损 concat)"))),
            ("", b("steps", "asr", QCheckBox("3. 语音识别自动生成字幕 (.srt)"))),
            ("", b("steps", "burn_subtitles", QCheckBox("4. 烧录字幕并附加封面图片 (.完整版.mp4)"))),
            ("", self.btn_custom),
        ]), "步骤")

        # 3. 编码设置
        self.encoder_combo = _combo(ENCODERS)
        self.preset_combo = QComboBox()
        self.encoder_combo.currentIndexChanged.connect(self._on_encoder_changed)
        tabs.addTab(_form_page([
            ("视频编码器", b("encode", "encoder", self.encoder_combo)),
            ("编码预设", b("encode", "preset", self.preset_combo)),
            ("弹幕烧录画质 (CQ)", b("encode", "danmaku_cq", _spin(0, 51))),
            ("成片画质 (CQ)", b("encode", "final_cq", _spin(0, 51))),
            ("音频码率", b("encode", "audio_bitrate", _combo(AUDIO_BITRATES))),
            ("", QLabel("💡 提示：CQ 数值越小画质越高、体积越大。弹幕烧录为中间过渡阶段，建议比成片画质更高（CQ 更小）。")),
        ]), "编码")

        # 4. 弹幕样式
        tabs.addTab(_form_page([
            ("弹幕字体", b("danmaku", "font", QFontComboBox())),
            ("字号倍率", b("danmaku", "font_scale", _dspin(0.5, 4.0, 0.1, 2))),
            ("可见度", b("danmaku", "opacity", _spin(0, 100, " %"))),
            ("滚动弹幕时长", b("danmaku", "scroll_duration", _dspin(1, 30, 0.5, 1, " 秒"))),
            ("静止弹幕时长", b("danmaku", "static_duration", _dspin(1, 30, 0.5, 1, " 秒"))),
            ("显示区域上限", b("danmaku", "area_ratio", _dspin(0.05, 1.0, 0.05, 2, " 高度"))),
        ]), "弹幕")

        # 5. 字幕样式
        tabs.addTab(_form_page([
            ("字幕字体", b("subtitle", "font", QFontComboBox())),
            ("字号比例", b("subtitle", "size_ratio", _dspin(0.01, 0.2, 0.005, 3, " 高度"))),
            ("描边宽度", b("subtitle", "outline", _dspin(0, 10, 0.5, 1, " 像素"))),
            ("底边间距", b("subtitle", "margin_ratio", _dspin(0, 0.3, 0.01, 3, " 高度"))),
            ("文字主色", b("subtitle", "primary_color", ColorButton())),
            ("描边颜色", b("subtitle", "outline_color", ColorButton())),
            ("每行最大字数", b("subtitle", "max_chars", _spin(6, 80))),
        ]), "字幕")

        # 6. 语音识别
        self.asr_engine_combo = _combo(ASR_ENGINES)
        self.quality_model_combo = _combo(QUALITY_MODELS)
        self.asr_language_combo = _combo(ASR_LANGUAGES)
        self.asr_context_edit = QLineEdit()
        self.asr_context_edit.setPlaceholderText("可填主播名、角色名、热词专业词或录播主题")
        self.asr_device_combo = _combo(ASR_DEVICES)
        self.asr_chunk_seconds = _dspin(30, 120, 30, 0, " 秒")
        tabs.addTab(_form_page([
            ("识别方案", b("asr", "engine", self.asr_engine_combo)),
            ("高精度模型", b("asr", "quality_model", self.quality_model_combo)),
            ("高精度语言", b("asr", "language", self.asr_language_combo)),
            ("专属词汇提示", b("asr", "context", self.asr_context_edit)),
            ("快捷设备（仅 Paraformer）", b("asr", "device", self.asr_device_combo)),
            ("分块时长（仅 Paraformer）", b("asr", "chunk_seconds", self.asr_chunk_seconds)),
            ("", b("asr", "reuse_existing_srt", QCheckBox("已有同名 .srt 时跳过识别（方便手动改字幕后直接重烤）"))),
            ("", QLabel("💡 高精度模式采用 CPU 完整精度并全程本地运行。Qwen3 支持语言与词汇提示；FireRedASR2-AED 不支持时会禁用对应项。\n首次运行会自动下载所选模型。若已有旧字幕，取消“跳过识别”可重新生成。")),
        ]), "识别")
        self.asr_engine_combo.currentIndexChanged.connect(self._on_asr_controls_changed)
        self.quality_model_combo.currentIndexChanged.connect(self._on_asr_controls_changed)
        self._on_asr_controls_changed()

        return tabs

    def _build_log_box(self) -> QGroupBox:
        box = QGroupBox("运行日志")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 14, 10, 8)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_MAX_LINES)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("Consolas", 9)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.log_view.setFont(font)
        layout.addWidget(self.log_view)
        return box

    def _build_run_bar(self) -> QWidget:
        container = QFrame()
        container.setObjectName("BottomBar")
        bar = QHBoxLayout(container)
        bar.setContentsMargins(12, 8, 12, 8)
        bar.setSpacing(10)

        # 核心步骤按钮 1
        self.btn_start = QPushButton("① 生成字幕")
        self.btn_start.setProperty("variant", "primary")
        self.btn_start.setMinimumWidth(130)
        self.btn_start.setMinimumHeight(38)
        self.btn_start.setToolTip("执行烧录弹幕、拼接视频及语音识别，导出 .srt 字幕供校对修改")

        # 核心步骤按钮 2
        self.btn_burn_subtitles = QPushButton("② 烤入成片")
        self.btn_burn_subtitles.setProperty("variant", "secondary")
        self.btn_burn_subtitles.setMinimumWidth(130)
        self.btn_burn_subtitles.setMinimumHeight(38)
        self.btn_burn_subtitles.setToolTip("读取用户校对后的 .srt 字幕，合成附带封面的完整版视频")

        # 取消按钮
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setProperty("variant", "danger")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setMinimumHeight(38)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setMinimumHeight(24)

        # 状态 Chip
        self.step_label = QLabel("就绪")
        self.step_label.setObjectName("StatusChip")
        self.step_label.setMinimumWidth(180)
        self.step_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 辅助功能按钮
        self.btn_edit_srt = QPushButton("修改字幕 (记事本)")
        self.btn_edit_srt.setEnabled(False)
        self.btn_edit_srt.setMinimumHeight(34)
        self.btn_edit_srt.setToolTip("用记事本打开生成的 SRT 字幕文件")

        self.btn_open = QPushButton("打开输出目录")
        self.btn_open.setEnabled(False)
        self.btn_open.setMinimumHeight(34)
        self.btn_open.setToolTip("在文件管理器中打开输出目录")

        bar.addWidget(self.btn_start)
        bar.addWidget(self.btn_burn_subtitles)
        bar.addWidget(self.btn_cancel)
        bar.addWidget(self.progress_bar, 1)
        bar.addWidget(self.step_label)
        bar.addWidget(self.btn_edit_srt)
        bar.addWidget(self.btn_open)

        self.btn_start.clicked.connect(self.generate_subtitles)
        self.btn_burn_subtitles.clicked.connect(self.burn_subtitles)
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_edit_srt.clicked.connect(self._edit_srt)
        self.btn_open.clicked.connect(self._open_output_dir)

        return container

    # ------------------------------------------------------------ 流程状态更新

    def _update_workflow_steps(self) -> None:
        """更新顶部流程导航高亮。"""
        segs = self.segments()
        has_segs = len(segs) > 0
        has_srt = self._last_srt_path is not None and self._last_srt_path.is_file()

        # Step 0: 导入分段
        self.step_pills[0].setProperty("active", "false" if has_segs else "true")
        self.step_pills[0].setProperty("completed", "true" if has_segs else "false")

        # Step 1: 生成字幕
        self.step_pills[1].setProperty("active", "true" if has_segs and not has_srt else "false")
        self.step_pills[1].setProperty("completed", "true" if has_srt else "false")

        # Step 2: 校对字幕
        self.step_pills[2].setProperty("active", "true" if has_srt else "false")
        self.step_pills[2].setProperty("completed", "false")

        # Step 3: 烤入成片
        self.step_pills[3].setProperty("active", "false")
        self.step_pills[3].setProperty("completed", "false")

        for pill in self.step_pills:
            pill.style().unpolish(pill)
            pill.style().polish(pill)

    # ------------------------------------------------------------ 设置管理

    def _on_encoder_changed(self) -> None:
        encoder = self.encoder_combo.currentData() or ""
        current = self.preset_combo.currentData()
        nvenc = "nvenc" in encoder
        presets = NVENC_PRESETS if nvenc else X264_PRESETS
        self.preset_combo.clear()
        for value, label in presets:
            self.preset_combo.addItem(label, value)
        default = "p5" if nvenc else "medium"
        index = self.preset_combo.findData(current)
        self.preset_combo.setCurrentIndex(index if index >= 0 else self.preset_combo.findData(default))

    def _on_asr_controls_changed(self) -> None:
        """Expose only controls supported by the selected recognition path."""
        if not hasattr(self, "asr_engine_combo"):
            return
        is_quality = self.asr_engine_combo.currentData() == "quality"
        self.quality_model_combo.setEnabled(is_quality)
        self.asr_device_combo.setEnabled(not is_quality)
        self.asr_chunk_seconds.setEnabled(not is_quality)
        supports_context = False
        supports_language = False
        if is_quality:
            try:
                profile = get_asr_profile(self.quality_model_combo.currentData() or "qwen3")
            except ValueError:
                profile = None
            if profile is not None:
                supports_context = profile.supports_context
                supports_language = profile.supports_language
        self.asr_context_edit.setEnabled(supports_context)
        self.asr_language_combo.setEnabled(supports_language)
        if not supports_context:
            self.asr_context_edit.setToolTip("所选识别方案不支持词汇提示；已有 Qwen 偏好会保存在设置中。")
        else:
            self.asr_context_edit.setToolTip("仅在支持词汇提示的识别模型中使用。")
        if not supports_language:
            self.asr_language_combo.setToolTip("所选识别方案不支持强制语言；已有 Qwen 偏好会保存在设置中。")
        else:
            self.asr_language_combo.setToolTip("所选模型支持强制识别语言。")

    def _apply_settings(self) -> None:
        s = self._settings
        _set_widget_value(self.encoder_combo, s.encode.encoder)
        self._on_encoder_changed()
        for bind in self._bindings:
            target = getattr(s, bind.section) if bind.section else s
            try:
                _set_widget_value(bind.widget, getattr(target, bind.field))
            except (AttributeError, TypeError, ValueError):
                pass
        self._on_asr_controls_changed()

    def _store_settings(self) -> None:
        s = self._settings
        for bind in self._bindings:
            target = getattr(s, bind.section) if bind.section else s
            value = _get_widget_value(bind.widget)
            if value is not None:
                setattr(target, bind.field, value)
        s.last_output_base = self.prefix_edit.text().strip()
        s.prefix_user_edited = self._prefix_user_edited
        s.window_geometry = bytes(self.saveGeometry().toBase64().data()).decode("ascii")
        s.theme = self._theme_mgr.current_theme()

    def _save_settings(self) -> None:
        self._store_settings()
        try:
            save_settings(self._settings, self._settings_file)
        except Exception as exc:  # noqa: BLE001
            self.append_log(f"保存设置失败：{exc}")

    def _restore_geometry(self) -> None:
        if self._settings.window_geometry:
            try:
                self.restoreGeometry(QByteArray.fromBase64(self._settings.window_geometry.encode("ascii")))
            except Exception:  # noqa: BLE001
                pass

    @property
    def settings(self) -> Settings:
        self._store_settings()
        return self._settings

    # ------------------------------------------------------------ 分段与资源

    def segments(self) -> list[Segment]:
        return [self.tree.topLevelItem(i).data(0, SEGMENT_ROLE) for i in range(self.tree.topLevelItemCount())]

    def is_probing(self) -> bool:
        return self._pending_scans > 0 or bool(self._pending_probes)

    def add_paths(self, paths: list) -> None:
        paths = [Path(p) for p in paths]
        if not paths:
            return
        folder = paths[0] if paths[0].is_dir() else paths[0].parent
        self._settings.last_folder = str(folder)
        self._pending_scans += 1
        self._update_summary()
        self._pool.start(_ScanTask(paths, self._signals))

    def _on_scanned(self, payload: tuple[list[Segment], list[Path]], error: str) -> None:
        self._pending_scans -= 1
        if error:
            self.append_log(error)
        segments, resources = payload
        existing = {self._path_key(seg.video) for seg in self.segments()}
        restore_keys = ({self._path_key(Path(p)) for p in self._restore_order}
                        if self._restore_order is not None else None)
        added = 0
        for seg in segments:
            key = self._path_key(seg.video)
            if key in existing or (restore_keys is not None and key not in restore_keys):
                continue
            existing.add(key)
            self._apply_saved_assignment(seg)
            self._add_segment_item(seg)
            added += 1
            for path in (seg.video, seg.xml, seg.cover):
                if path is not None:
                    self._add_resource_item(path)
        for path in resources:
            self._add_resource_item(path)
        self._auto_match_resources()
        self._refresh_resource_links()
        if not error:
            sidecars = sum(resource_kind(p) != "video" for p in resources)
            self.append_log(f"已添加 {added} 个视频分段，检测到 {sidecars} 个关联资源"
                            if resources else "未找到可用的视频、XML 或图片文件")
            if sidecars and not segments:
                self.workspace_tabs.setCurrentIndex(1)
        if self._restore_order is not None:
            preferred = {self._path_key(Path(p)): idx for idx, p in enumerate(self._restore_order)}
            items = [self.tree.takeTopLevelItem(0) for _ in range(self.tree.topLevelItemCount())]
            items.sort(key=lambda item: preferred.get(
                self._path_key(item.data(0, SEGMENT_ROLE).video), len(preferred)))
            for item in items:
                self.tree.addTopLevelItem(item)
            self._restore_order = None
        self._on_segments_changed()

    def _apply_saved_assignment(self, segment: Segment) -> None:
        overrides = self._settings.sidecar_assignments.get(self._path_key(segment.video), {})
        for kind in ("xml", "cover"):
            if kind in overrides:
                setattr(segment, kind, Path(overrides[kind]) if overrides[kind] else None)

    def _set_manual_assignment(self, segment: Segment, kind: str, path: Path | None) -> None:
        setattr(segment, kind, path)
        key = self._path_key(segment.video)
        self._settings.sidecar_assignments.setdefault(key, {})[kind] = str(path) if path else ""

    @staticmethod
    def _sidecar_stem(path: Path) -> str:
        name = path.name.casefold()
        for suffix in (".cover.jpg", ".cover.jpeg", ".cover.png", ".cover.webp",
                       ".jpg", ".jpeg", ".png", ".webp", ".xml"):
            if name.endswith(suffix):
                return name[:-len(suffix)]
        return path.stem.casefold()

    def _auto_match_resources(self) -> None:
        sidecars = [Path(item.data(0, SEGMENT_ROLE)) for item in self._resources.values()
                    if resource_kind(Path(item.data(0, SEGMENT_ROLE))) in ("xml", "cover")
                    and Path(item.data(0, SEGMENT_ROLE)).is_file()]
        stem_counts: dict[str, int] = {}
        for segment in self.segments():
            stem = segment.video.stem.casefold()
            stem_counts[stem] = stem_counts.get(stem, 0) + 1
        for segment in self.segments():
            overrides = self._settings.sidecar_assignments.get(self._path_key(segment.video), {})
            for kind in ("xml", "cover"):
                if kind in overrides or getattr(segment, kind) is not None:
                    continue
                candidates = [p for p in sidecars
                              if resource_kind(p) == kind
                              and self._sidecar_stem(p) == segment.video.stem.casefold()]
                if (len(candidates) == 1
                        and (candidates[0].parent == segment.video.parent
                             or stem_counts[segment.video.stem.casefold()] == 1)):
                    setattr(segment, kind, candidates[0])
                    self.append_log(f"自动配对{RESOURCE_LABELS[kind]}：{segment.video.name} ← {candidates[0].name}")
            item = next((item for item in self._items.values()
                         if item.data(0, SEGMENT_ROLE) is segment), None)
            if item is not None:
                self._fill_item(item, segment)

    def _add_resource_item(self, path: Path) -> None:
        kind = resource_kind(path)
        if kind is None:
            return
        key = self._path_key(path)
        if key in self._resources:
            return
        item = QTreeWidgetItem()
        item.setData(0, SEGMENT_ROLE, Path(path))
        item.setText(0, RESOURCE_LABELS[kind])
        item.setText(1, path.name)
        item.setToolTip(1, str(path))
        if not path.is_file():
            item.setForeground(0, QColor("#ef4444"))
            item.setText(2, "文件缺失")
        self.resource_tree.addTopLevelItem(item)
        self._resources[key] = item

    def _refresh_resource_links(self) -> None:
        segments = self.segments()
        for key, item in self._resources.items():
            path: Path = item.data(0, SEGMENT_ROLE)
            kind = resource_kind(path)
            if not path.is_file():
                item.setText(2, "⚠ 文件缺失")
            elif kind == "video":
                item.setText(2, "✓ 参与排序" if any(self._path_key(s.video) == key for s in segments)
                             else "— 未加入")
            else:
                linked = [s.video.name for s in segments
                          if (s.xml if kind == "xml" else s.cover) is not None
                          and self._path_key(s.xml if kind == "xml" else s.cover) == key]
                item.setText(2, f"✓ 已关联 ({len(linked)})" if linked else "— 未关联")
                item.setToolTip(2, "、".join(linked) if linked else "未关联")

    @staticmethod
    def _path_key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(path))

    def _add_segment_item(self, seg: Segment) -> None:
        token = self._next_token
        self._next_token += 1
        item = QTreeWidgetItem()
        item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                      | Qt.ItemFlag.ItemIsDragEnabled)
        item.setData(0, SEGMENT_ROLE, seg)
        item.setData(0, TOKEN_ROLE, token)
        self.tree.addTopLevelItem(item)
        self._items[token] = item
        self._fill_item(item, seg)
        if seg.info is None and seg.error is None:
            self._pending_probes.add(token)
            self._pool.start(_ProbeTask(token, seg.video, self._signals))

    def _on_probed(self, token: int, info: MediaInfo | None, error: str) -> None:
        self._pending_probes.discard(token)
        item = self._items.get(token)
        if item is not None:
            seg: Segment = item.data(0, SEGMENT_ROLE)
            seg.info = info
            seg.error = error or None
            self._fill_item(item, seg)
            if error:
                self.append_log(f"读取分段媒体失败：{seg.video.name}：{error}")
        self._update_summary()
        if not self.is_probing():
            self.tree.header().resizeSections(QHeaderView.ResizeMode.ResizeToContents)

    def _fill_item(self, item: QTreeWidgetItem, seg: Segment) -> None:
        item.setText(1, seg.video.name)
        item.setToolTip(1, str(seg.video))
        item.setText(2, seg.start_time.strftime("%Y-%m-%d %H:%M:%S") if seg.start_time else "—")
        item.setText(5, "✓ 已配对" if seg.xml and seg.xml.is_file() else "— 未配对")
        item.setToolTip(5, str(seg.xml) if seg.xml else "未找到同名弹幕，该分段将不加弹幕")
        item.setText(6, "✓ 已配对" if seg.cover and seg.cover.is_file() else "— 未配对")
        item.setToolTip(6, str(seg.cover) if seg.cover else "无关联封面")
        item.setText(7, fmt_size(seg.size))

        if seg.info is not None:
            item.setText(COL_DURATION, fmt_duration(seg.info.duration))
            item.setText(COL_RESOLUTION, f"{seg.info.width}×{seg.info.height}")
            item.setToolTip(COL_DURATION, "")
        elif seg.error:
            item.setText(COL_DURATION, "读取失败")
            item.setText(COL_RESOLUTION, "—")
            item.setToolTip(COL_DURATION, seg.error)
        else:
            item.setText(COL_DURATION, "探测中…")
            item.setText(COL_RESOLUTION, "")

        brush = QColor("#ef4444") if seg.error else None
        for col in range(len(COLUMNS)):
            if seg.error:
                item.setForeground(col, brush)
            else:
                item.setData(col, Qt.ItemDataRole.ForegroundRole, None)

    def _on_segments_changed(self) -> None:
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setText(0, str(i + 1))
        target_key = self.resource_target.currentData()
        self.resource_target.clear()
        for segment in self.segments():
            self.resource_target.addItem(segment.video.name, self._path_key(segment.video))
        previous_index = self.resource_target.findData(target_key)
        if previous_index >= 0:
            self.resource_target.setCurrentIndex(previous_index)
        self._settings.last_segments = [str(s.video) for s in self.segments()]
        self._settings.last_resources = [str(self.resource_tree.topLevelItem(i).data(0, SEGMENT_ROLE))
                                         for i in range(self.resource_tree.topLevelItemCount())]
        if not self._prefix_user_edited:
            segs = self.segments()
            self.prefix_edit.setText(default_output_base(segs) if segs else "")
        self._update_summary()
        self._refresh_resource_links()
        self._update_workflow_steps()

    def _update_summary(self) -> None:
        segs = self.segments()
        total = sum(s.info.duration for s in segs if s.info is not None)
        text = f"共 {len(segs)} 段 · 总时长 {fmt_duration(total)}"
        if self.is_probing():
            text += " (读取中…)"
        self.summary_label.setText(text)
        self._update_output_hint()

    def _update_output_hint(self) -> None:
        if not hasattr(self, "output_hint"):
            return
        base = self.prefix_edit.text().strip()
        self.output_hint.setText(
            f"💡 流程：先生成 {base}.srt，校对修改后再烤入 {base}.完整版.mp4。"
            if base else "")
        segments = self.segments() if hasattr(self, "tree") else []
        out_text = self.output_dir_edit.text().strip()
        output_dir = Path(out_text) if out_text else (segments[0].video.parent if segments else None)
        if output_dir is not None and base:
            self._last_output_dir = output_dir
            self._last_srt_path = output_dir / f"{base}.srt"
        else:
            self._last_srt_path = None
        if hasattr(self, "btn_edit_srt") and not self.is_running():
            self.btn_edit_srt.setEnabled(
                self._last_srt_path is not None and self._last_srt_path.is_file())
        if hasattr(self, "btn_open") and not self.is_running():
            self.btn_open.setEnabled(output_dir is not None and output_dir.is_dir())
        self._update_workflow_steps()

    def _on_prefix_edited(self, text: str) -> None:
        self._prefix_user_edited = bool(text.strip())
        if not self._prefix_user_edited:
            self._on_segments_changed()

    def _selected_rows(self) -> list[int]:
        return sorted(self.tree.indexOfTopLevelItem(item) for item in self.tree.selectedItems())

    def _sync_target_to_selection(self) -> None:
        rows = self._selected_rows()
        if len(rows) == 1:
            segment: Segment = self.tree.topLevelItem(rows[0]).data(0, SEGMENT_ROLE)
            index = self.resource_target.findData(self._path_key(segment.video))
            if index >= 0:
                self.resource_target.setCurrentIndex(index)

    def _selected_resource_paths(self) -> list[Path]:
        return [item.data(0, SEGMENT_ROLE) for item in self.resource_tree.selectedItems()]

    def _add_selected_videos_to_order(self) -> None:
        existing = {self._path_key(seg.video) for seg in self.segments()}
        added = 0
        for path in self._selected_resource_paths():
            key = self._path_key(path)
            if resource_kind(path) != "video" or key in existing or not path.is_file():
                continue
            segment = make_segment(path, probe_media=False)
            self._apply_saved_assignment(segment)
            self._add_segment_item(segment)
            existing.add(key)
            added += 1
        if added:
            self._auto_match_resources()
            self._on_segments_changed()
            self._save_settings()

    def _link_selected_resources(self) -> None:
        sidecars = [p for p in self._selected_resource_paths() if resource_kind(p) in ("xml", "cover")]
        target_key = self.resource_target.currentData()
        if target_key is None or not sidecars:
            self._message(QMessageBox.Icon.Warning, "请选择资源与目标视频",
                          "请在资源页选中 XML 和/或封面图片，并在下拉框中选择要配对的目标视频。")
            return
        kinds = [resource_kind(p) for p in sidecars]
        if len(kinds) != len(set(kinds)) or any(not p.is_file() for p in sidecars):
            self._message(QMessageBox.Icon.Warning, "无法关联",
                          "同一类型每次只能选一个文件，且文件必须真实存在。")
            return
        item = next((item for item in self._items.values()
                     if self._path_key(item.data(0, SEGMENT_ROLE).video) == target_key), None)
        if item is None:
            self._message(QMessageBox.Icon.Warning, "目标视频未找到", "请重新选择目标视频。")
            return
        segment: Segment = item.data(0, SEGMENT_ROLE)
        for path, kind in zip(sidecars, kinds):
            self._set_manual_assignment(segment, kind, path)
            self.append_log(f"手动配对关联{RESOURCE_LABELS[kind]}：{segment.video.name} ← {path.name}")
        self._fill_item(item, segment)
        self._refresh_resource_links()
        self._save_settings()

    def _unlink_selected_resources(self) -> None:
        selected = {self._path_key(p) for p in self._selected_resource_paths()
                    if resource_kind(p) in ("xml", "cover")}
        if not selected:
            self._message(QMessageBox.Icon.Warning, "请选择资源", "请先在资源列表中选中要取消关联的文件。")
            return
        changed = False
        for item in self._items.values():
            segment: Segment = item.data(0, SEGMENT_ROLE)
            for kind in ("xml", "cover"):
                path = getattr(segment, kind)
                if path is not None and self._path_key(path) in selected:
                    self._set_manual_assignment(segment, kind, None)
                    changed = True
            if changed:
                self._fill_item(item, segment)
        if changed:
            self._refresh_resource_links()
            self._save_settings()

    def _remove_selected_resources(self) -> None:
        selected = self._selected_resource_paths()
        if not selected:
            return
        keys = {self._path_key(p) for p in selected}
        for item in list(self._items.values()):
            segment: Segment = item.data(0, SEGMENT_ROLE)
            if self._path_key(segment.video) in keys:
                token = item.data(0, TOKEN_ROLE)
                self._items.pop(token, None)
                self._pending_probes.discard(token)
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
                self._settings.sidecar_assignments.pop(self._path_key(segment.video), None)
                continue
            for kind in ("xml", "cover"):
                path = getattr(segment, kind)
                if path is not None and self._path_key(path) in keys:
                    self._set_manual_assignment(segment, kind, None)
                    self._fill_item(item, segment)
        for path in selected:
            key = self._path_key(path)
            item = self._resources.pop(key, None)
            if item is not None:
                self.resource_tree.takeTopLevelItem(self.resource_tree.indexOfTopLevelItem(item))
        self._on_segments_changed()
        self._save_settings()

    def _move_selected(self, delta: int) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        count = self.tree.topLevelItemCount()
        if (delta < 0 and rows[0] == 0) or (delta > 0 and rows[-1] == count - 1):
            return
        items = [self.tree.topLevelItem(r) for r in rows]
        for row in (rows if delta < 0 else reversed(rows)):
            item = self.tree.takeTopLevelItem(row)
            self.tree.insertTopLevelItem(row + delta, item)
        self.tree.clearSelection()
        for item in items:
            item.setSelected(True)
        self.tree.setCurrentItem(items[0])
        self._on_segments_changed()

    def remove_selected(self) -> None:
        for row in sorted(self._selected_rows(), reverse=True):
            item = self.tree.takeTopLevelItem(row)
            token = item.data(0, TOKEN_ROLE)
            self._items.pop(token, None)
            self._pending_probes.discard(token)
        self._on_segments_changed()

    def clear_segments(self) -> None:
        self.tree.clear()
        self._items.clear()
        self._pending_probes.clear()
        self._on_segments_changed()

    def sort_by_time(self) -> None:
        items = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        by_key = {self._path_key(it.data(0, SEGMENT_ROLE).video): it for it in items}
        ordered = sort_segments([it.data(0, SEGMENT_ROLE) for it in items])
        while self.tree.topLevelItemCount():
            self.tree.takeTopLevelItem(0)
        for seg in ordered:
            self.tree.addTopLevelItem(by_key[self._path_key(seg.video)])
        self._on_segments_changed()

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择录播所在的文件夹", self._settings.last_folder)
        if folder:
            self.add_paths([Path(folder)])

    def _choose_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择视频、弹幕 XML 或封面图片", self._settings.last_folder,
            "录播资源 (*.flv *.mp4 *.mkv *.ts *.xml *.jpg *.jpeg *.png *.webp);;"
            "视频 (*.flv *.mp4 *.mkv *.ts);;弹幕 (*.xml);;"
            "图片 (*.jpg *.jpeg *.png *.webp);;所有文件 (*.*)")
        if files:
            self.add_paths([Path(f) for f in files])

    def _choose_output_dir(self) -> None:
        start = self.output_dir_edit.text() or self._settings.last_folder
        folder = QFileDialog.getExistingDirectory(self, "选择成品输出文件夹", start)
        if folder:
            self.output_dir_edit.setText(str(Path(folder)))

    # ------------------------------------------------------------ 日志与提示

    def append_log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{stamp}] {text.rstrip()}")
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _message(self, icon: QMessageBox.Icon, title: str, text: str) -> QMessageBox:
        box = QMessageBox(icon, title, text, QMessageBox.StandardButton.Ok, self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()
        return box

    def check_ffmpeg(self) -> bool:
        try:
            find_tool("ffmpeg")
            find_tool("ffprobe")
        except FfmpegError as exc:
            self.append_log(f"未检测到 ffmpeg/ffprobe：{exc}")
            self._message(QMessageBox.Icon.Critical, "缺少 FFmpeg 核心组件",
                          "未在系统中找到 ffmpeg / ffprobe，无法处理音视频。\n\n"
                          "请安装 FFmpeg 并将其所在目录加入系统环境变量 PATH，"
                          "或将 ffmpeg.exe 与 ffprobe.exe 放置于本程序同级的 bin 目录中。")
            return False
        return True

    # ------------------------------------------------------------ 任务运行控制

    def is_running(self) -> bool:
        return self._job_controller.is_running

    def _validate(self, segs: list[Segment], steps=None) -> str | None:
        if not segs:
            return "请先添加至少一个录播视频分段。"
        if self.is_probing():
            return "正在探测视频时长与分辨率参数，请稍候片刻再开始。"
        bad = [s for s in segs if s.info is None or s.error]
        if bad:
            names = "\n".join(f"· {s.video.name}：{s.error or '未读取到视频信息'}" for s in bad[:10])
            return f"以下分段媒体无法读取或存在损坏，请移除后再开始：\n{names}"
        steps = steps or self._settings.steps
        if not (steps.burn_danmaku or steps.concat or steps.asr or steps.burn_subtitles):
            return "请至少勾选一个处理流水线步骤。"
        base = self.prefix_edit.text().strip() or default_output_base(segs)
        if not base or any(c in INVALID_NAME_CHARS for c in base):
            return '文件名前缀不能为空，且不能包含 \\ / : * ? " < > | 等非法字符。'
        return None

    def start(self) -> None:
        """按设置勾选的高级步骤运行。"""
        self._start_job(None)

    def generate_subtitles(self) -> None:
        """阶段一：分段弹幕烧录、拼接、识别语音，生成 SRT。"""
        self._start_job("generate")

    def burn_subtitles(self) -> None:
        """阶段二：读取用户校对后的 SRT，烤入合成最终完整版。"""
        self._start_job("burn")

    def _start_job(self, mode: str | None) -> None:
        if self.is_running():
            return
        self._store_settings()
        if not self.check_ffmpeg():
            return
        segs = self.segments()
        out_text = self.output_dir_edit.text().strip()
        output_dir = Path(out_text) if out_text else (segs[0].video.parent if segs else Path())
        base = self.prefix_edit.text().strip() or default_output_base(segs)
        job = self._job_controller.build_job(segs, self._settings, output_dir, base, mode) if segs else None

        problem = self._validate(segs, job.settings.steps if job else None)
        if problem:
            self._message(QMessageBox.Icon.Warning, "校验未通过", problem)
            return

        assert job is not None
        if mode == "burn" and not (job.output_dir / f"{job.output_base}.srt").is_file():
            self._message(QMessageBox.Icon.Warning, "缺少字幕文件",
                          "未找到对应的字幕文件，请先点击“① 生成字幕”，并在检查修改 SRT 后再烤入成片。")
            return

        try:
            job.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._message(QMessageBox.Icon.Warning, "无法开始", f"无法创建输出目录：\n{job.output_dir}\n{exc}")
            return

        self._save_settings()
        self._last_output_dir = job.output_dir
        self._last_srt_path = job.output_dir / f"{job.output_base}.srt"
        self._current_mode = mode

        self.btn_open.setEnabled(False)
        self.btn_edit_srt.setEnabled(False)
        self.progress_bar.setValue(0)
        self.step_label.setText("▶ 准备中…")
        self.append_log(f"开始处理 {len(segs)} 个分段 → {job.output_dir}（前缀：{job.output_base}）")

        if not self._job_controller.start(job, mode):
            return

    def cancel(self) -> None:
        if self._job_controller.cancel():
            self.btn_cancel.setEnabled(False)
            self.step_label.setText("■ 正在终止…")
            self.append_log("收到取消指令，正在等待当前任务平稳退出…")

    def _set_running(self, running: bool) -> None:
        for widget in (*self._segment_buttons, self.tree, self.resource_tree,
                       self.resource_target, self.tabs,
                       self.btn_start, self.btn_burn_subtitles, self.prefix_edit):
            widget.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        self.btn_edit_srt.setEnabled(
            not running and self._last_srt_path is not None and self._last_srt_path.is_file())

    def _on_progress(self, value: float) -> None:
        self.progress_bar.setValue(max(0, min(1000, int(round(value * 1000)))))

    def _on_finished(self, outputs: list) -> None:
        self.progress_bar.setValue(1000)
        self.step_label.setText("✓ 处理完成")
        self.append_log("全部任务顺利完成！产物列表：")
        for path in outputs:
            self.append_log(f"  · {path}")
        if self._current_mode == "generate":
            self.append_log("💡 提示：可以在输出目录校对修改 SRT 字幕，完成后点击“② 烤入成片”。")
        self.btn_open.setEnabled(self._last_output_dir is not None)
        self._update_workflow_steps()
        QApplication.alert(self)

    def _on_failed(self, message: str) -> None:
        self.step_label.setText("✕ 处理失败")
        self.append_log(f"处理发生错误：{message}")
        if not self._close_pending:
            self._message(QMessageBox.Icon.Critical, "处理失败", message[-2000:])

    def _on_cancelled(self) -> None:
        self.step_label.setText("■ 已取消")
        self.append_log("任务已取消。")

    def _on_job_running_changed(self, running: bool) -> None:
        self._set_running(running)
        if running:
            return
        self._update_workflow_steps()
        if self._close_pending:
            QTimer.singleShot(0, self.close)

    def _open_output_dir(self) -> None:
        if self._last_output_dir is not None and self._last_output_dir.exists():
            os.startfile(self._last_output_dir)  # noqa: S606 - Windows 平台

    def _edit_srt(self) -> None:
        if self._last_srt_path is not None and self._last_srt_path.is_file():
            subprocess.Popen(["notepad.exe", str(self._last_srt_path)])

    # ------------------------------------------------------------ 退出处理

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.is_running():
            if not self._close_pending:
                answer = QMessageBox.question(
                    self, "确认退出", "后台正在处理音视频任务，确定要终止任务并退出吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
                self._close_pending = True
                self.cancel()
            event.ignore()
            return
        self._save_settings()
        self._pool.clear()
        self._pool.waitForDone(5000)
        event.accept()


# ---------------------------------------------------------------- 程序入口

def _install_excepthook() -> None:
    def hook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            log = settings_path().parent / "crash.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}]\n{text}")
        except OSError:
            pass
        if QApplication.instance() is not None:
            QMessageBox.critical(None, APP_TITLE, f"程序遇到未捕获的严重错误：\n\n{text[-3000:]}")

    sys.excepthook = hook


def run_app() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setOrganizationName("LuboStudio")
    _install_excepthook()

    window = MainWindow()
    window.show()
    QTimer.singleShot(0, window.check_ffmpeg)
    return app.exec()
