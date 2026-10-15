"""录播助手 (LuboStudio) 现代主题与样式系统。

支持暗色 (Dark) 与亮色 (Light) 主题无缝切换，提供圆角卡片、平滑渐变、流程指示器与现代控件样式。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from PySide6.QtCore import QEasingCurve, QObject, QPropertyAnimation, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect, QWidget


@dataclass
class ThemePalette:
    name: str
    background: str
    surface: str
    surface_variant: str
    surface_hover: str
    primary: str
    primary_hover: str
    secondary: str
    secondary_hover: str
    success: str
    warning: str
    error: str
    text_primary: str
    text_secondary: str
    text_disabled: str
    border: str
    separator: str
    scrollbar: str
    scrollbar_hover: str
    selection: str
    progress_gradient_start: str
    progress_gradient_end: str


DARK_PALETTE = ThemePalette(
    name="dark",
    background="#141622",
    surface="#1c1f2e",
    surface_variant="#25293d",
    surface_hover="#2f344d",
    primary="#38bdf8",
    primary_hover="#0ea5e9",
    secondary="#a855f7",
    secondary_hover="#9333ea",
    success="#22c55e",
    warning="#f59e0b",
    error="#ef4444",
    text_primary="#f8fafc",
    text_secondary="#94a3b8",
    text_disabled="#64748b",
    border="#2c314a",
    separator="#222538",
    scrollbar="#383e5c",
    scrollbar_hover="#4d557d",
    selection="rgba(56, 189, 248, 0.22)",
    progress_gradient_start="#38bdf8",
    progress_gradient_end="#a855f7",
)

LIGHT_PALETTE = ThemePalette(
    name="light",
    background="#f4f6f9",
    surface="#ffffff",
    surface_variant="#eef2f6",
    surface_hover="#e2e8f0",
    primary="#0284c7",
    primary_hover="#0369a1",
    secondary="#7c3aed",
    secondary_hover="#6d28d9",
    success="#16a34a",
    warning="#d97706",
    error="#dc2626",
    text_primary="#0f172a",
    text_secondary="#64748b",
    text_disabled="#94a3b8",
    border="#d8e1ea",
    separator="#e2e8f0",
    scrollbar="#cbd5e1",
    scrollbar_hover="#94a3b8",
    selection="rgba(2, 132, 199, 0.16)",
    progress_gradient_start="#0284c7",
    progress_gradient_end="#7c3aed",
)

# 选用在全平台与各种 Windows 字体下均能完美渲染的符号
ICONS: Dict[str, str] = {
    "app": "▶",
    "folder": "📂",
    "file": "📄",
    "up": "▲",
    "down": "▼",
    "remove": "✕",
    "clear": "⟲",
    "sort": "⏱",
    "play": "▶",
    "stop": "■",
    "open": "↗",
    "edit": "✎",
    "settings": "⚙",
    "sun": "☀",
    "moon": "☾",
    "link": "🔗",
    "unlink": "⛓",
    "add": "+",
    "burn": "🔥",
    "subtitle": "💬",
    "check": "✓",
    "warning": "⚠",
    "error": "✕",
    "info": "ℹ",
    "stats": "📊",
    "asr": "🤖",
    "danmaku": "🗯",
    "video": "🎬",
    "ready": "●",
}


def icon_text(name: str) -> str:
    """返回常用动作的文本图标。"""
    return ICONS.get(name, "•")


def fade_transition(widget: QWidget, duration: int = 220) -> None:
    """切换主题时的轻微淡入过渡动画。"""
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)

    anim = QPropertyAnimation(effect, b"opacity", widget)
    widget._fade_anim = anim
    anim.setDuration(duration)
    anim.setStartValue(0.5)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.finished.connect(lambda: widget.setGraphicsEffect(None))
    anim.start()


def generate_qss(p: ThemePalette) -> str:
    """根据主题调色盘生成现代化 Qt 样式表。"""
    is_dark = p.name == "dark"

    return f"""
/* ==============================================================
   全局基础
============================================================== */
* {{
    font-family: "Segoe UI", "Microsoft YaHei", "PingFang SC", "Segoe UI Emoji", sans-serif;
    color: {p.text_primary};
    selection-background-color: {p.selection};
    selection-color: {p.text_primary};
    outline: none;
}}

QMainWindow, QDialog {{
    background-color: {p.background};
}}

QWidget {{
    background-color: transparent;
}}

/* ==============================================================
   顶部栏与流程导航条
============================================================== */
#AppHeader {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 10px;
    padding: 6px 14px;
}}

#HeaderLogo {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                stop:0 {p.primary}, stop:1 {p.secondary});
    color: #ffffff;
    font-size: 15px;
    font-weight: 800;
    border-radius: 8px;
    padding: 4px 8px;
    min-width: 20px;
    min-height: 20px;
}}

#HeaderTitle {{
    font-size: 15px;
    font-weight: 700;
    color: {p.text_primary};
}}

#HeaderSubtitle {{
    font-size: 11px;
    color: {p.text_secondary};
}}

#StepBar {{
    background-color: {p.surface_variant};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 3px 6px;
}}

.step-pill {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 3px 10px;
    font-size: 12px;
    font-weight: 600;
    color: {p.text_secondary};
}}

.step-pill[active="true"] {{
    background-color: {p.surface};
    border: 1px solid {p.primary};
    color: {p.primary};
}}

.step-pill[completed="true"] {{
    background-color: transparent;
    border: 1px solid transparent;
    color: {p.success};
}}

#ThemeToggleBtn {{
    background-color: {p.surface_variant};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 600;
    color: {p.text_primary};
}}

#ThemeToggleBtn:hover {{
    background-color: {p.surface_hover};
    border-color: {p.primary};
}}

/* ==============================================================
   卡片分组容器 (QGroupBox)
============================================================== */
QGroupBox {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 10px;
    margin-top: 20px;
    padding: 14px 10px 10px 10px;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 2px 8px;
    background-color: {p.surface_variant};
    border: 1px solid {p.border};
    border-radius: 5px;
    color: {p.primary};
    font-size: 12px;
    font-weight: 700;
}}

/* ==============================================================
   按钮 (QPushButton)
============================================================== */
QPushButton {{
    background-color: {p.surface_variant};
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 500;
    color: {p.text_primary};
}}

QPushButton:hover {{
    background-color: {p.surface_hover};
    border-color: {p.border};
}}

QPushButton:pressed {{
    background-color: {p.separator};
}}

QPushButton:disabled {{
    background-color: {p.surface};
    color: {p.text_disabled};
    border: 1px solid {p.separator};
}}

/* 核心行动按钮 (① 生成字幕) */
QPushButton[variant="primary"],
QPushButton[class="primary"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 {p.primary}, stop:1 {p.primary_hover});
    color: #ffffff;
    border: none;
    font-weight: 700;
    font-size: 13px;
    padding: 7px 18px;
    border-radius: 7px;
}}

QPushButton[variant="primary"]:hover,
QPushButton[class="primary"]:hover {{
    background: {p.primary_hover};
}}

QPushButton[variant="primary"]:disabled,
QPushButton[class="primary"]:disabled {{
    background: {p.surface_variant};
    color: {p.text_disabled};
}}

/* 次要核心按钮 (② 烤入成片) */
QPushButton[variant="secondary"],
QPushButton[class="secondary"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 {p.secondary}, stop:1 {p.secondary_hover});
    color: #ffffff;
    border: none;
    font-weight: 700;
    font-size: 13px;
    padding: 7px 18px;
    border-radius: 7px;
}}

QPushButton[variant="secondary"]:hover,
QPushButton[class="secondary"]:hover {{
    background: {p.secondary_hover};
}}

QPushButton[variant="secondary"]:disabled,
QPushButton[class="secondary"]:disabled {{
    background: {p.surface_variant};
    color: {p.text_disabled};
}}

/* 危险/取消按钮 */
QPushButton[variant="danger"],
QPushButton[class="danger"] {{
    background-color: {p.error};
    color: #ffffff;
    border: none;
    font-weight: 600;
    border-radius: 7px;
}}

QPushButton[variant="danger"]:hover,
QPushButton[class="danger"]:hover {{
    background-color: #dc2626;
}}

QPushButton[variant="danger"]:disabled,
QPushButton[class="danger"]:disabled {{
    background: {p.surface};
    color: {p.text_disabled};
    border: 1px solid {p.separator};
}}

/* ==============================================================
   标签页 (QTabWidget / QTabBar)
============================================================== */
QTabWidget::pane {{
    border: 1px solid {p.border};
    border-radius: 8px;
    background-color: {p.surface};
    top: -1px;
    padding: 4px;
}}

QTabBar::tab {{
    background-color: {p.background};
    color: {p.text_secondary};
    padding: 6px 14px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    border: 1px solid transparent;
    font-size: 12px;
    font-weight: 600;
    margin-right: 3px;
}}

QTabBar::tab:hover {{
    color: {p.text_primary};
    background-color: {p.surface_variant};
}}

QTabBar::tab:selected {{
    color: {p.primary};
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-bottom: 2px solid {p.primary};
}}

/* ==============================================================
   树形列表 / 表格 (QTreeWidget)
============================================================== */
QTreeWidget {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 8px;
    alternate-background-color: {p.surface_variant};
    outline: none;
    font-size: 12px;
}}

QTreeWidget::item {{
    height: 30px;
    padding: 1px 4px;
    border-radius: 4px;
}}

QTreeWidget::item:hover {{
    background-color: {p.surface_hover};
}}

QTreeWidget::item:selected {{
    background-color: {p.selection};
    color: {p.primary};
}}

QHeaderView::section {{
    background-color: {p.surface_variant};
    color: {p.text_secondary};
    font-weight: 600;
    font-size: 12px;
    padding: 6px 6px;
    border: none;
    border-right: 1px solid {p.border};
    border-bottom: 1px solid {p.border};
}}

QHeaderView::section:first {{
    border-top-left-radius: 8px;
}}

QHeaderView::section:last {{
    border-top-right-radius: 8px;
    border-right: none;
}}

/* ==============================================================
   输入框、下拉框、调节框
============================================================== */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QFontComboBox {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 5px 8px;
    color: {p.text_primary};
    font-size: 12px;
    selection-background-color: {p.primary};
}}

QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QFontComboBox:focus {{
    border: 1px solid {p.primary};
}}

QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    background-color: {p.surface_variant};
    color: {p.text_disabled};
    border-color: {p.separator};
}}

QComboBox::drop-down {{
    border: none;
    width: 22px;
}}

QComboBox::down-arrow {{
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p.text_secondary};
    margin-right: 8px;
}}

QComboBox QAbstractItemView {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 6px;
    outline: none;
    padding: 4px;
    selection-background-color: {p.selection};
}}

/* ==============================================================
   复选框 (QCheckBox)
============================================================== */
QCheckBox {{
    spacing: 8px;
    color: {p.text_primary};
    font-size: 12px;
}}

QCheckBox::indicator {{
    width: 17px;
    height: 17px;
    border: 1px solid {p.border};
    border-radius: 4px;
    background-color: {p.surface};
}}

QCheckBox::indicator:hover {{
    border: 1px solid {p.primary};
}}

QCheckBox::indicator:checked {{
    background-color: {p.primary};
    border: 1px solid {p.primary};
}}

QCheckBox::indicator:disabled {{
    background-color: {p.surface_variant};
    border-color: {p.separator};
}}

/* ==============================================================
   进度条 (QProgressBar)
============================================================== */
QProgressBar {{
    background-color: {p.surface_variant};
    border: 1px solid {p.border};
    border-radius: 8px;
    text-align: center;
    color: {p.text_primary};
    font-size: 12px;
    font-weight: 700;
    min-height: 20px;
}}

QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 {p.progress_gradient_start},
                                stop:1 {p.progress_gradient_end});
    border-radius: 7px;
}}

/* ==============================================================
   日志与多行文本 (QPlainTextEdit)
============================================================== */
QPlainTextEdit {{
    background-color: {p.surface};
    color: {p.text_primary};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 6px;
    font-size: 11px;
}}

/* ==============================================================
   分割条 (QSplitter)
============================================================== */
QSplitter::handle {{
    background-color: {p.separator};
    margin: 1px;
}}

QSplitter::handle:hover {{
    background-color: {p.primary};
}}

QSplitter::handle:horizontal {{
    width: 3px;
}}

QSplitter::handle:vertical {{
    height: 3px;
}}

/* ==============================================================
   滚动条 (QScrollBar)
============================================================== */
QScrollBar:vertical {{
    background-color: transparent;
    width: 8px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background-color: {p.scrollbar};
    min-height: 24px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical:hover {{
    background-color: {p.scrollbar_hover};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QScrollBar:horizontal {{
    background-color: transparent;
    height: 8px;
    margin: 0;
}}

QScrollBar::handle:horizontal {{
    background-color: {p.scrollbar};
    min-width: 24px;
    border-radius: 4px;
}}

QScrollBar::handle:horizontal:hover {{
    background-color: {p.scrollbar_hover};
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* ==============================================================
   标签与浮窗 (QLabel, QToolTip)
============================================================== */
QLabel {{
    color: {p.text_primary};
    font-size: 12px;
}}

QToolTip {{
    background-color: {p.surface};
    color: {p.text_primary};
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 5px 8px;
    font-size: 12px;
}}

#SummaryBadge {{
    background-color: {p.surface_variant};
    border: 1px solid {p.border};
    border-radius: 10px;
    padding: 3px 10px;
    font-size: 12px;
    font-weight: 600;
    color: {p.text_secondary};
}}

#StatusChip {{
    background-color: {p.surface_variant};
    border: 1px solid {p.border};
    border-radius: 7px;
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 600;
    color: {p.text_primary};
}}

#BottomBar {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 10px;
    padding: 8px 12px;
}}
"""


class ThemeManager(QObject):
    """主题管理器：管理当前主题状态、调色板与全局 QSS 注入。"""

    themeChanged = Signal(str)

    def __init__(self, app: QApplication, initial_theme: str = "dark") -> None:
        super().__init__()
        self._app = app
        self._current_theme = initial_theme if initial_theme in ("dark", "light") else "dark"
        self.set_theme(self._current_theme)

    def current_theme(self) -> str:
        return self._current_theme

    def set_theme(self, theme: str) -> None:
        if theme not in ("dark", "light"):
            theme = "dark"
        self._current_theme = theme
        qss = self.stylesheet()
        self._app.setStyleSheet(qss)

        pal = self._app.palette()
        p = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(p.background))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(p.text_primary))
        pal.setColor(QPalette.ColorRole.Base, QColor(p.surface))
        pal.setColor(QPalette.ColorRole.AlternateBase, QColor(p.surface_variant))
        pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(p.surface_variant))
        pal.setColor(QPalette.ColorRole.ToolTipText, QColor(p.text_primary))
        pal.setColor(QPalette.ColorRole.Text, QColor(p.text_primary))
        pal.setColor(QPalette.ColorRole.Button, QColor(p.surface_variant))
        pal.setColor(QPalette.ColorRole.ButtonText, QColor(p.text_primary))
        pal.setColor(QPalette.ColorRole.Highlight, QColor(p.primary))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        self._app.setPalette(pal)

        self.themeChanged.emit(self._current_theme)

    def toggle_theme(self) -> None:
        new_theme = "light" if self._current_theme == "dark" else "dark"
        self.set_theme(new_theme)

    def palette(self) -> ThemePalette:
        return DARK_PALETTE if self._current_theme == "dark" else LIGHT_PALETTE

    def stylesheet(self) -> str:
        return generate_qss(self.palette())
