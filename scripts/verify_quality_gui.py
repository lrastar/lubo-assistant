"""Offscreen GUI/configuration check against the bundled application modules."""
import json
import os
from pathlib import Path
import sys
import traceback

root = Path(os.environ["LUBO_GUI_CHECK_DIR"])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(Path(sys.executable).parent / "PySide6" / "plugins")
sys.stdout = sys.stderr = open(root / "quality-gui-check.log", "w", encoding="utf-8")
try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont, QFontDatabase
    from lubo.config import load_settings
    from lubo.gui.main_window import MainWindow, _get_widget_value, _set_widget_value
    app = QApplication([])
    font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
    families = QFontDatabase.applicationFontFamilies(font_id)
    if families:
        app.setFont(QFont(families[0], 9))
    settings_file = root / "quality-gui-check.settings.json"
    window = MainWindow(settings_file=settings_file)
    window.resize(1280, 900)
    bindings = {(b.section, b.field): b.widget for b in window._bindings}
    assert _get_widget_value(bindings['asr', 'engine']) == 'quality'
    assert _get_widget_value(bindings['asr', 'language']) == ''
    _set_widget_value(bindings['asr', 'context'], '主播名、角色名、专业词')
    for index in range(window.tabs.count()):
        if window.tabs.tabText(index) == '识别':
            window.tabs.setCurrentIndex(index)
    window.show()
    app.processEvents()
    window.grab().save(str(root / "quality-gui.png"))
    _set_widget_value(bindings['asr', 'engine'], 'paraformer')
    _set_widget_value(bindings['asr', 'language'], 'Chinese')
    window._save_settings()
    saved = load_settings(settings_file)
    assert saved.asr.engine == 'paraformer' and saved.asr.language == 'Chinese'
    assert saved.asr.context == '主播名、角色名、专业词'
    window.close()
    result = {'ok': True, 'engines': ['quality', 'paraformer'], 'language_and_context_roundtrip': True}
except BaseException:
    result = {'ok': False, 'error': traceback.format_exc()}
(root / "quality-gui-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
sys.stdout.flush()
os._exit(0 if result['ok'] else 1)
