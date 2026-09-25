"""主窗口：分段列表、设置、运行控制与日志。"""
from __future__ import annotations

import copy
import os
import subprocess
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, QRunnable, QThread, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent, QColor, QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox,
    QFileDialog, QFontComboBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from lubo.config import Settings, load_settings, save_settings, settings_path
from lubo.ffmpeg import FfmpegError, MediaInfo, find_tool, probe
from lubo.pipeline import Job
from lubo.segments import (
    VIDEO_SUFFIXES, Segment, default_output_base, is_source_video, make_segment, sort_segments,
)
from lubo.gui.worker import PipelineWorker

APP_TITLE = "录播助手"
SEGMENT_ROLE = Qt.ItemDataRole.UserRole
TOKEN_ROLE = Qt.ItemDataRole.UserRole + 1
LOG_MAX_LINES = 5000
COLUMNS = ("序号", "文件名", "开始时间", "时长", "分辨率", "弹幕", "封面", "大小")
COL_DURATION, COL_RESOLUTION = 3, 4
INVALID_NAME_CHARS = set('<>:"/\\|?*')
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
RESOURCE_LABELS = {"video": "视频", "xml": "弹幕 XML", "cover": "封面图片"}

ENCODERS = [("h264_nvenc", "H.264（NVENC 显卡）"), ("hevc_nvenc", "HEVC（NVENC 显卡）"),
            ("libx264", "H.264（x264 CPU）")]
NVENC_PRESETS = [(f"p{i}", f"p{i}" + {1: "（最快）", 5: "（推荐）", 7: "（最慢/质量最好）"}.get(i, ""))
                 for i in range(1, 8)]
X264_PRESETS = [(p, p) for p in ("ultrafast", "superfast", "veryfast", "faster", "fast",
                                 "medium", "slow", "slower", "veryslow")]
AUDIO_BITRATES = [(b, b) for b in ("128k", "160k", "192k", "256k", "320k")]
ASR_DEVICES = [("cuda", "显卡（CUDA）"), ("cpu", "CPU")]


# ---------------------------------------------------------------- 格式化

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


# ---------------------------------------------------------------- 后台任务

class _TaskSignals(QObject):
    scanned = Signal(object, str)          # list[Segment], 错误信息
    probed = Signal(int, object, str)      # token, MediaInfo | None, 错误信息


def resource_kind(path: Path) -> str | None:
    if path.suffix.lower() in VIDEO_SUFFIXES:
        return "video"
    if path.suffix.lower() == ".xml":
        return "xml"
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return "cover"
    return None


def expand_resource_paths(paths: list[Path]) -> list[Path]:
    """文件夹展开为视频、弹幕和图片；导入顺序不影响右侧视频排序。"""
    resources: list[Path] = []
    for path in paths:
        if path.is_dir():
            entries = sorted(p for p in path.iterdir()
                             if p.is_file() and resource_kind(p))
            # 原片和烧录成片共存时只自动选原片；只有成片时也允许导入。
            if any(is_source_video(p) for p in entries):
                entries = [p for p in entries
                           if resource_kind(p) != "video" or is_source_video(p)]
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
            segments = [make_segment(path, probe_media=False)
                        for path in resources if resource_kind(path) == "video"]
            self._signals.scanned.emit((sort_segments(segments), resources), "")
        except Exception as exc:  # noqa: BLE001
            self._signals.scanned.emit(([], []), f"扫描失败：{exc}")


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


# ---------------------------------------------------------------- 小部件

class SegmentTree(QTreeWidget):
    """支持内部拖拽排序，以及从资源管理器拖入文件/文件夹。"""

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
    """列出视频/XML/图片，并接受资源管理器拖入。"""

    filesDropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setColumnCount(3)
        self.setHeaderLabels(["类型", "文件名", "关联视频"])
        self.setRootIsDecorated(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.header().resizeSection(2, 140)

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
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls()
                 if url.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.filesDropped.emit(paths)
        else:
            super().dropEvent(event)


class ColorButton(QPushButton):
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
        text_color = "#000000" if self._color.lightness() > 128 else "#FFFFFF"
        self.setText(self.color())
        self.setStyleSheet(f"background-color: {self.color()}; color: {text_color};")


@dataclass
class _Binding:
    section: str        # Settings 的子对象名；"" 表示 Settings 本身
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
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    for label, widget in rows:
        if label:
            form.addRow(label, widget)
        else:
            form.addRow(widget)
    return page


# ---------------------------------------------------------------- 主窗口

class MainWindow(QMainWindow):
    def __init__(self, settings_file: Path | None = None) -> None:
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
        self._thread: QThread | None = None
        self._worker: PipelineWorker | None = None
        self._close_pending = False
        self._last_output_dir: Path | None = None
        self._last_srt_path: Path | None = None

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)
        self._signals = _TaskSignals(self)
        self._signals.scanned.connect(self._on_scanned)
        self._signals.probed.connect(self._on_probed)

        self.setWindowTitle(APP_TITLE)
        self.resize(1350, 820)
        self._build_ui()
        self._apply_settings()
        if self._settings.last_output_base:
            self.prefix_edit.setText(self._settings.last_output_base)
            self._prefix_user_edited = self._settings.prefix_user_edited
        self._restore_geometry()
        self._update_summary()
        restored = self._settings.last_resources or self._settings.last_segments
        if restored:
            self._restore_order = list(self._settings.last_segments)
            self.add_paths([Path(path) for path in restored])

    # ------------------------------------------------------------ 界面构建

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._build_workspace_box())
        lower = QSplitter(Qt.Orientation.Horizontal)
        lower.addWidget(self._build_settings_tabs())
        lower.addWidget(self._build_log_box())
        lower.setStretchFactor(0, 1)
        lower.setStretchFactor(1, 1)
        splitter.addWidget(lower)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(splitter, 1)
        layout.addLayout(self._build_run_bar())
        self.setCentralWidget(central)

    def _build_workspace_box(self) -> QGroupBox:
        box = QGroupBox("分段（可拖动排序，可从资源管理器拖入视频、XML、图片或文件夹）")
        layout = QVBoxLayout(box)
        toolbar = QHBoxLayout()
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
        self.summary_label = QLabel()
        toolbar.addWidget(self.summary_label)
        layout.addLayout(toolbar)

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

    def _build_resource_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        actions = QHBoxLayout()
        self.btn_add_to_order = QPushButton("视频加入排序")
        self.resource_target = QComboBox()
        self.resource_target.setMinimumWidth(220)
        self.btn_link_resource = QPushButton("关联到目标视频")
        self.btn_unlink_resource = QPushButton("取消关联")
        self.btn_remove_resource = QPushButton("移除资源")
        actions.addWidget(self.btn_add_to_order)
        actions.addSpacing(12)
        actions.addWidget(QLabel("目标视频"))
        actions.addWidget(self.resource_target, 1)
        actions.addWidget(self.btn_link_resource)
        actions.addWidget(self.btn_unlink_resource)
        actions.addWidget(self.btn_remove_resource)
        layout.addLayout(actions)
        self.resource_tree = ResourceTree()
        layout.addWidget(self.resource_tree)
        hint = QLabel("同名 XML/封面会自动配对；名称不同可选中资源、选择目标视频后点击“关联到目标视频”。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.resource_tree.filesDropped.connect(self.add_paths)
        return pane

    def _build_order_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
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

    def _bind(self, section: str, field: str, widget: QWidget) -> QWidget:
        self._bindings.append(_Binding(section, field, widget))
        return widget

    def _build_settings_tabs(self) -> QTabWidget:
        tabs = QTabWidget()
        self.tabs = tabs
        b = self._bind

        # 输出
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText("留空 = 第一个分段所在文件夹")
        self.output_dir_edit.textChanged.connect(self._update_output_hint)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._choose_output_dir)
        dir_row = QWidget()
        dir_layout = QHBoxLayout(dir_row)
        dir_layout.setContentsMargins(0, 0, 0, 0)
        dir_layout.addWidget(self.output_dir_edit, 1)
        dir_layout.addWidget(browse)
        self.prefix_edit = QLineEdit()
        self.prefix_edit.setPlaceholderText("默认 = 第一个分段的文件名")
        self.prefix_edit.textEdited.connect(self._on_prefix_edited)
        self.prefix_edit.textChanged.connect(self._update_output_hint)
        self.output_hint = QLabel()
        self.output_hint.setWordWrap(True)
        self.output_hint.setStyleSheet("color: gray;")
        b("", "output_dir", self.output_dir_edit)
        tabs.addTab(_form_page([
            ("输出文件夹", dir_row),
            ("文件名前缀", self.prefix_edit),
            ("", self.output_hint),
            ("", b("", "keep_intermediate", QCheckBox("保留中间文件（烧录分段、合并视频、wav 等）"))),
            ("", b("", "overwrite", QCheckBox("覆盖已有文件（不勾选则复用已存在的产物，便于中断后续跑）"))),
        ]), "输出")

        # 步骤
        self.btn_custom = QPushButton("按勾选步骤运行（高级）")
        self.btn_custom.clicked.connect(self.start)
        tabs.addTab(_form_page([
            ("", b("steps", "burn_danmaku", QCheckBox("1. 每段烧录弹幕"))),
            ("", b("steps", "concat", QCheckBox("2. 拼接所有分段"))),
            ("", b("steps", "asr", QCheckBox("3. 语音识别生成字幕"))),
            ("", b("steps", "burn_subtitles", QCheckBox("4. 烧录字幕并附加封面"))),
            ("", self.btn_custom),
        ]), "步骤")

        # 编码
        self.encoder_combo = _combo(ENCODERS)
        self.preset_combo = QComboBox()
        self.encoder_combo.currentIndexChanged.connect(self._on_encoder_changed)
        tabs.addTab(_form_page([
            ("编码器", b("encode", "encoder", self.encoder_combo)),
            ("预设", b("encode", "preset", self.preset_combo)),
            ("弹幕烧录质量 (CQ)", b("encode", "danmaku_cq", _spin(0, 51))),
            ("成品质量 (CQ)", b("encode", "final_cq", _spin(0, 51))),
            ("音频码率", b("encode", "audio_bitrate", _combo(AUDIO_BITRATES))),
            ("", QLabel("CQ 越小画质越高、文件越大；弹幕烧录是中间文件，建议比成品略小。")),
        ]), "编码")

        # 弹幕
        tabs.addTab(_form_page([
            ("字体", b("danmaku", "font", QFontComboBox())),
            ("字号倍率", b("danmaku", "font_scale", _dspin(0.5, 4.0, 0.1, 2))),
            ("可见度", b("danmaku", "opacity", _spin(0, 100, " %"))),
            ("滚动弹幕时长", b("danmaku", "scroll_duration", _dspin(1, 30, 0.5, 1, " 秒"))),
            ("顶部/底部弹幕时长", b("danmaku", "static_duration", _dspin(1, 30, 0.5, 1, " 秒"))),
            ("显示区域（占画面高度）", b("danmaku", "area_ratio", _dspin(0.05, 1.0, 0.05, 2))),
        ]), "弹幕")

        # 字幕
        tabs.addTab(_form_page([
            ("字体", b("subtitle", "font", QFontComboBox())),
            ("字号（占画面高度）", b("subtitle", "size_ratio", _dspin(0.01, 0.2, 0.005, 3))),
            ("描边（1080p 基准）", b("subtitle", "outline", _dspin(0, 10, 0.5, 1))),
            ("底边距（占画面高度）", b("subtitle", "margin_ratio", _dspin(0, 0.3, 0.01, 3))),
            ("文字颜色", b("subtitle", "primary_color", ColorButton())),
            ("描边颜色", b("subtitle", "outline_color", ColorButton())),
            ("每条最多字数", b("subtitle", "max_chars", _spin(6, 80))),
        ]), "字幕")

        # 识别
        tabs.addTab(_form_page([
            ("运行设备", b("asr", "device", _combo(ASR_DEVICES))),
            ("识别分块时长", b("asr", "chunk_seconds", _dspin(30, 120, 30, 0, " 秒"))),
            ("", b("asr", "reuse_existing_srt", QCheckBox("已有同名 .srt 时跳过识别（便于手动修改字幕后重烧）"))),
            ("", QLabel("首次识别会自动从 ModelScope 下载模型（约 2–3 GB），请保持联网。")),
        ]), "识别")
        return tabs

    def _build_log_box(self) -> QGroupBox:
        box = QGroupBox("日志")
        layout = QVBoxLayout(box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_MAX_LINES)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.log_view.setFont(font)
        layout.addWidget(self.log_view)
        return box

    def _build_run_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        self.btn_start = QPushButton("① 生成字幕")
        self.btn_start.setMinimumWidth(110)
        self.btn_burn_subtitles = QPushButton("② 烤入成片")
        self.btn_burn_subtitles.setMinimumWidth(110)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
        self.btn_open = QPushButton("打开输出文件夹")
        self.btn_open.setEnabled(False)
        self.btn_edit_srt = QPushButton("用记事本改字幕")
        self.btn_edit_srt.setEnabled(False)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.step_label = QLabel("就绪")
        self.step_label.setMinimumWidth(220)
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
        return bar

    # ------------------------------------------------------------ 设置绑定

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

    def _apply_settings(self) -> None:
        s = self._settings
        # 编码器需先设置，以便预设下拉框填充正确的选项
        _set_widget_value(self.encoder_combo, s.encode.encoder)
        self._on_encoder_changed()
        for bind in self._bindings:
            target = getattr(s, bind.section) if bind.section else s
            try:
                _set_widget_value(bind.widget, getattr(target, bind.field))
            except (AttributeError, TypeError, ValueError):
                pass

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

    # ------------------------------------------------------------ 分段列表

    def segments(self) -> list[Segment]:
        """界面当前顺序下的分段（直接取自各行保存的 Segment 对象）。"""
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
        restore_keys = ({self._path_key(Path(path)) for path in self._restore_order}
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
            sidecars = sum(resource_kind(path) != "video" for path in resources)
            self.append_log(f"已添加 {added} 个视频分段，扫描到 {sidecars} 个 XML/图片资源"
                            if resources else "没有找到可用的视频、XML 或图片文件")
            if sidecars and not segments:
                self.workspace_tabs.setCurrentIndex(1)
        if self._restore_order is not None:
            preferred = {self._path_key(Path(path)): index
                         for index, path in enumerate(self._restore_order)}
            items = [self.tree.takeTopLevelItem(0)
                     for _ in range(self.tree.topLevelItemCount())]
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
                candidates = [path for path in sidecars
                              if resource_kind(path) == kind
                              and self._sidecar_stem(path) == segment.video.stem.casefold()]
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
            item.setForeground(0, QColor("#C62828"))
            item.setText(2, "文件缺失")
        self.resource_tree.addTopLevelItem(item)
        self._resources[key] = item

    def _refresh_resource_links(self) -> None:
        segments = self.segments()
        for key, item in self._resources.items():
            path: Path = item.data(0, SEGMENT_ROLE)
            kind = resource_kind(path)
            if not path.is_file():
                item.setText(2, "文件缺失")
            elif kind == "video":
                item.setText(2, "参与排序" if any(self._path_key(s.video) == key for s in segments)
                             else "未加入")
            else:
                linked = [s.video.name for s in segments
                          if (s.xml if kind == "xml" else s.cover) is not None
                          and self._path_key(s.xml if kind == "xml" else s.cover) == key]
                item.setText(2, "、".join(linked) if linked else "未关联")
                item.setToolTip(2, "\n".join(linked))

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
                self.append_log(f"读取失败：{seg.video.name}：{error}")
        self._update_summary()
        if not self.is_probing():
            self.tree.header().resizeSections(QHeaderView.ResizeMode.ResizeToContents)

    def _fill_item(self, item: QTreeWidgetItem, seg: Segment) -> None:
        item.setText(1, seg.video.name)
        item.setToolTip(1, str(seg.video))
        item.setText(2, seg.start_time.strftime("%Y-%m-%d %H:%M:%S") if seg.start_time else "—")
        item.setText(5, "已配对" if seg.xml and seg.xml.is_file() else "未配对")
        item.setToolTip(5, str(seg.xml) if seg.xml else "没有同名弹幕文件，该段将不加弹幕")
        item.setText(6, "已配对" if seg.cover and seg.cover.is_file() else "未配对")
        item.setToolTip(6, str(seg.cover) if seg.cover else "")
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
            item.setText(COL_DURATION, "读取中…")
            item.setText(COL_RESOLUTION, "")
        brush = QColor("#C62828") if seg.error else QColor()
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
        self._settings.last_segments = [str(segment.video) for segment in self.segments()]
        self._settings.last_resources = [str(self.resource_tree.topLevelItem(i).data(0, SEGMENT_ROLE))
                                         for i in range(self.resource_tree.topLevelItemCount())]
        if not self._prefix_user_edited:
            segs = self.segments()
            self.prefix_edit.setText(default_output_base(segs) if segs else "")
        self._update_summary()
        self._refresh_resource_links()

    def _update_summary(self) -> None:
        segs = self.segments()
        total = sum(s.info.duration for s in segs if s.info is not None)
        text = f"共 {len(segs)} 段，总时长 {fmt_duration(total)}"
        if self.is_probing():
            text += "（读取中…）"
        self.summary_label.setText(text)
        self._update_output_hint()

    def _update_output_hint(self) -> None:
        if not hasattr(self, "output_hint"):
            return
        base = self.prefix_edit.text().strip()
        self.output_hint.setText(
            f"先生成 {base}.srt，修改保存后再烤入 {base}.完整版.mp4。"
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

    def _on_prefix_edited(self, text: str) -> None:
        # 用户清空前缀后恢复自动填充
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
        sidecars = [path for path in self._selected_resource_paths()
                    if resource_kind(path) in ("xml", "cover")]
        target_key = self.resource_target.currentData()
        if target_key is None or not sidecars:
            self._message(QMessageBox.Icon.Warning, "请选择资源和视频",
                          "请在资源页选中 XML 和/或封面图片，并选择目标视频。")
            return
        kinds = [resource_kind(path) for path in sidecars]
        if len(kinds) != len(set(kinds)) or any(not path.is_file() for path in sidecars):
            self._message(QMessageBox.Icon.Warning, "无法关联",
                          "同一类型每次只能选一个文件，且文件必须存在。")
            return
        item = next((item for item in self._items.values()
                     if self._path_key(item.data(0, SEGMENT_ROLE).video) == target_key), None)
        if item is None:
            self._message(QMessageBox.Icon.Warning, "找不到目标视频", "请重新选择目标视频。")
            return
        segment: Segment = item.data(0, SEGMENT_ROLE)
        for path, kind in zip(sidecars, kinds):
            self._set_manual_assignment(segment, kind, path)
            self.append_log(f"手动关联{RESOURCE_LABELS[kind]}：{segment.video.name} ← {path.name}")
        self._fill_item(item, segment)
        self._refresh_resource_links()
        self._save_settings()

    def _unlink_selected_resources(self) -> None:
        selected = {self._path_key(path) for path in self._selected_resource_paths()
                    if resource_kind(path) in ("xml", "cover")}
        if not selected:
            self._message(QMessageBox.Icon.Warning, "请选择资源", "请先在资源页选中要取消关联的 XML 或封面图片。")
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
        keys = {self._path_key(path) for path in selected}
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
        folder = QFileDialog.getExistingDirectory(self, "选择录播文件夹", self._settings.last_folder)
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
        folder = QFileDialog.getExistingDirectory(self, "选择输出文件夹", start)
        if folder:
            self.output_dir_edit.setText(str(Path(folder)))

    # ------------------------------------------------------------ 日志与提示

    def append_log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{stamp}] {text.rstrip()}")
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _message(self, icon: QMessageBox.Icon, title: str, text: str) -> QMessageBox:
        # 非阻塞弹窗：不卡住事件循环（后台信号照常处理）
        box = QMessageBox(icon, title, text, QMessageBox.StandardButton.Ok, self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()
        return box

    def check_ffmpeg(self) -> bool:
        try:
            find_tool("ffmpeg")
            find_tool("ffprobe")
        except FfmpegError as exc:
            self.append_log(f"未找到 ffmpeg：{exc}")
            self._message(QMessageBox.Icon.Critical, "缺少 ffmpeg",
                          "没有找到 ffmpeg / ffprobe，无法处理视频。\n\n"
                          "请安装 ffmpeg 并把它所在的文件夹加入系统 PATH，"
                          "或者把 ffmpeg.exe 和 ffprobe.exe 放到本软件目录下的 bin 文件夹中，然后重新打开软件。")
            return False
        return True

    # ------------------------------------------------------------ 运行

    def is_running(self) -> bool:
        return self._thread is not None

    def _validate(self, segs: list[Segment], steps=None) -> str | None:
        if not segs:
            return "请先添加至少一个分段。"
        if self.is_probing():
            return "仍在读取分段信息，请稍候再开始。"
        bad = [s for s in segs if s.info is None or s.error]
        if bad:
            names = "\n".join(f"· {s.video.name}：{s.error or '未读取到信息'}" for s in bad[:10])
            return f"以下分段无法读取，请移除后再开始：\n{names}"
        steps = steps or self._settings.steps
        if not (steps.burn_danmaku or steps.concat or steps.asr or steps.burn_subtitles):
            return "请至少勾选一个处理步骤。"
        base = self.prefix_edit.text().strip() or default_output_base(segs)
        if not base or any(c in INVALID_NAME_CHARS for c in base):
            return '文件名前缀不能为空，也不能包含 \\ / : * ? " < > | 等字符。'
        return None

    def _build_job(self, segs: list[Segment]) -> Job:
        out_text = self.output_dir_edit.text().strip()
        output_dir = Path(out_text) if out_text else segs[0].video.parent
        base = self.prefix_edit.text().strip() or default_output_base(segs)
        return Job(segments=list(segs), settings=copy.deepcopy(self._settings),
                   output_dir=output_dir, output_base=base)

    def start(self) -> None:
        """按高级步骤设置运行；保留给需要单步重做的用户。"""
        self._start_job(None)

    def generate_subtitles(self) -> None:
        """第一阶段：分段弹幕、拼接、识别；保留合并视频供校对后烤录。"""
        self._start_job("generate")

    def burn_subtitles(self) -> None:
        """第二阶段：读取用户改过的 SRT，烤入视频。"""
        self._start_job("burn")

    def _start_job(self, mode: str | None) -> None:
        if self.is_running():
            return
        self._store_settings()
        if not self.check_ffmpeg():
            return
        segs = self.segments()
        job = self._build_job(segs) if segs else None
        if job is not None and mode is not None:
            job.strict_srt_source = True
            job.settings.steps.burn_danmaku = True
            job.settings.steps.concat = True
            job.settings.steps.asr = mode == "generate"
            job.settings.steps.burn_subtitles = mode == "burn"
            if mode == "generate":
                job.settings.keep_intermediate = True
            else:
                job.settings.asr.reuse_existing_srt = True
        problem = self._validate(segs, job.settings.steps if job else None)
        if problem:
            self._message(QMessageBox.Icon.Warning, "无法开始", problem)
            return
        assert job is not None
        if mode == "burn" and not (job.output_dir / f"{job.output_base}.srt").is_file():
            self._message(QMessageBox.Icon.Warning, "缺少字幕",
                          "请先点击“生成字幕”，修改并保存 SRT 后再烤入成片。")
            return
        try:
            job.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._message(QMessageBox.Icon.Warning, "无法开始", f"无法创建输出文件夹：\n{job.output_dir}\n{exc}")
            return
        self._save_settings()

        self._last_output_dir = job.output_dir
        self._last_srt_path = job.output_dir / f"{job.output_base}.srt"
        self._current_mode = mode
        self.btn_open.setEnabled(False)
        self.btn_edit_srt.setEnabled(False)
        self.progress_bar.setValue(0)
        self.step_label.setText("准备中…")
        self.append_log(f"开始处理 {len(segs)} 个分段 → {job.output_dir}（前缀：{job.output_base}）")

        self._thread = QThread(self)
        self._worker = PipelineWorker(job)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self.append_log)
        self._worker.step.connect(self.step_label.setText)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        for sig in (self._worker.finished, self._worker.failed, self._worker.cancelled):
            sig.connect(self._thread.quit)
        self._thread.finished.connect(self._on_thread_finished)
        self._set_running(True)
        self._thread.start()

    def cancel(self) -> None:
        if self._worker is not None and not self._worker.is_cancelling:
            self._worker.cancel()
            self.btn_cancel.setEnabled(False)
            self.step_label.setText("正在取消…")
            self.append_log("正在取消，请稍候…")

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
        self.step_label.setText("已完成")
        self.append_log("全部完成。产物：")
        for path in outputs:
            self.append_log(f"  {path}")
        if self._current_mode == "generate":
            self.append_log("可在输出文件夹修改 SRT，保存后点击“② 烤入成片”。")
        self.btn_open.setEnabled(self._last_output_dir is not None)
        QApplication.alert(self)

    def _on_failed(self, message: str) -> None:
        self.step_label.setText("失败")
        self.append_log(f"处理失败：{message}")
        if not self._close_pending:
            self._message(QMessageBox.Icon.Critical, "处理失败", message[-2000:])

    def _on_cancelled(self) -> None:
        self.step_label.setText("已取消")
        self.append_log("已取消。")

    def _on_thread_finished(self) -> None:
        if self._thread is not None:
            self._thread.deleteLater()
        if self._worker is not None:
            self._worker.deleteLater()
        self._thread = None
        self._worker = None
        self._set_running(False)
        if self._close_pending:
            self.close()

    def _open_output_dir(self) -> None:
        if self._last_output_dir is not None and self._last_output_dir.exists():
            os.startfile(self._last_output_dir)  # noqa: S606 - Windows 专用

    def _edit_srt(self) -> None:
        if self._last_srt_path is not None and self._last_srt_path.is_file():
            subprocess.Popen(["notepad.exe", str(self._last_srt_path)])

    # ------------------------------------------------------------ 关闭

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.is_running():
            if not self._close_pending:
                answer = QMessageBox.question(
                    self, "确认退出", "任务仍在进行，确定要取消并退出吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
                self._close_pending = True
                self.cancel()
            # 等工作线程结束后由 _on_thread_finished 再次调用 close()
            event.ignore()
            return
        self._save_settings()
        self._pool.clear()
        self._pool.waitForDone(5000)
        event.accept()


# ---------------------------------------------------------------- 入口

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
            QMessageBox.critical(None, APP_TITLE, f"程序发生未处理的错误：\n\n{text[-3000:]}")

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
