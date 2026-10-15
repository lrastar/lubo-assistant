# -*- mode: python ; coding: utf-8 -*-
"""Windows onedir 发行配置；请通过 build_exe.ps1 调用。"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)


root = Path(SPECPATH).resolve()


def required_env(name):
    value = os.environ.get(name)
    if not value or not Path(value).is_file():
        raise RuntimeError(f"缺少 {name}；请运行 build_exe.ps1，并确认文件存在。")
    return str(Path(value).resolve())


ffmpeg = required_env("LUBO_FFMPEG_EXE")
ffprobe = required_env("LUBO_FFPROBE_EXE")
ffmpeg_license = required_env("LUBO_FFMPEG_LICENSE")

binaries = [
    (ffmpeg, "bin"),
    (ffprobe, "bin"),
]
datas = [
    (ffmpeg_license, "licenses/ffmpeg"),
    (str(root / "docs" / "FFMPEG_NOTICE.txt"), "licenses/ffmpeg"),
]
ffmpeg_readme = os.environ.get("LUBO_FFMPEG_README")
if ffmpeg_readme and Path(ffmpeg_readme).is_file():
    datas.append((ffmpeg_readme, "licenses/ffmpeg"))

# FunASR 在运行时按模型注册表动态加载实现；ModelScope 的 hub 下载器也
# 含动态导入。模型权重不打包，首次识别时仍按项目设置下载到用户缓存。
hiddenimports = collect_submodules("funasr")
hiddenimports += collect_submodules("modelscope.hub")
hiddenimports += collect_submodules("modelscope_hub")
hiddenimports += ["torchaudio", "soundfile"]
# Qwen3-ASR and its forced aligner are loaded by the isolated quality worker;
# keep their Transformers modules in the archive even though the main GUI does
# not import them until a worker starts. Jieba is optional at runtime, but its
# bundled dictionary makes Chinese subtitle splitting available offline.
hiddenimports += collect_submodules("transformers.models.qwen3_asr")
hiddenimports += collect_submodules("jieba")
datas += collect_data_files("jieba", excludes=["**/__pycache__/**"])
# FireRed runs in its own Python environment. These two support scripts are
# launched as files from quality_support and must not be folded into the GUI
# process or its Qwen dependency set.
for worker_script in ("firered_worker.py", "firered_runtime.py"):
    source = root / "scripts" / worker_script
    if not source.is_file():
        raise RuntimeError(f"缺少 FireRed 运行脚本：{source}")
    datas.append((str(source), "quality_support"))
# FunASR 的注册装饰器会 inspect.getsourcelines(模型类)；冻结包仅有
# PYZ 字节码时显式导入模型会失败，因此需要把其 .py 源码也放在包内。
datas += collect_data_files("funasr", include_py_files=True, excludes=["**/__pycache__/**"])
datas += collect_data_files("modelscope")
datas += collect_data_files("modelscope_hub")

for distribution in (
    "funasr", "modelscope", "modelscope-hub", "torch", "torchaudio",
    "huggingface-hub", "transformers", "accelerate", "jieba",
):
    try:
        datas += copy_metadata(distribution)
    except Exception:
        pass

a = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "PySide2"],
    noarchive=False,
)
# Qt6Core 使用 Windows 自带的无版本号 icuuc.dll。构建机 PATH 中的
# Poppler 也有同名 DLL，但只导出带版本号的 ICU 符号；PyInstaller 的
# 依赖扫描会误把它复制到程序根目录，覆盖系统 DLL，导致 QtCore 无法加载。
a.binaries = [entry for entry in a.binaries if entry[0].lower() != "icuuc.dll"]

# PyInstaller 的依赖扫描还会把 torch/lib 下的大型 DLL 再复制一份到程序
# 根目录。仅当 torch/lib 中已收集了同一个源文件时，去掉根目录副本。
def binary_key(entry):
    return entry[0].replace("\\", "/").lower(), os.path.normcase(entry[1])


nested_torch_dlls = {
    binary_key(entry)
    for entry in a.binaries
    if binary_key(entry)[0].startswith("torch/lib/")
}
a.binaries = [
    entry for entry in a.binaries
    if not (
        "/" not in binary_key(entry)[0]
        and binary_key(entry)[0].endswith(".dll")
        and ("torch/lib/" + binary_key(entry)[0], binary_key(entry)[1]) in nested_torch_dlls
    )
]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="录播助手",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    contents_directory=".",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="录播助手",
)
