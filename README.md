# 录播助手

Windows 本地桌面软件，处理录播姬断流产生的多个视频：配对弹幕 XML 和封面、按手动顺序拼接、识别语音生成可修改的 SRT，再把字幕烤入成片。使用 PySide6，不需要网页服务。

## 使用流程

1. 导入视频、弹幕 XML 和 JPG/PNG/WebP 封面。上方“视频分段”页签可拖动排序；“导入的资源”页签可检查配对关系，必要时选择目标视频手动关联。缺少 XML 或封面仍可继续。手动选择 `.烧录弹幕.GPU.mp4` 等已处理视频也能导入；添加文件夹时若有原片则优先选择原片，只有成片时也会导入。
2. 确认输出目录和编码设置，点击“① 生成字幕”。程序烧录弹幕、拼接分段并生成 SRT。损坏的视频会在处理前提示；音视频参数不同的分段会统一编码。
3. 修改并保存 SRT，点击“② 烤入成片”。成片和 SRT 默认放在第一段视频的目录。程序不会修改原始视频、XML 或封面。

## 从源码运行

需要 Windows、Python 3.10 和 FFmpeg/FFprobe。先安装 FFmpeg，并确保 `ffmpeg.exe`、`ffprobe.exe` 在 PATH 中；语音识别首次运行需要联网下载模型。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

以后可双击 `启动.bat`。NVIDIA 显卡可选择 NVENC 编码和 CUDA 识别；没有可用显卡时可在设置里选择 x264 和 CPU。语音识别使用 [FunASR](https://github.com/modelscope/FunASR) 的中文 Paraformer、FSMN-VAD 和标点模型；背景音乐可能影响识别，建议烤录前校对 SRT。

## 构建 EXE

安装依赖并准备 FFmpeg 后，在 PowerShell 运行 `./build_exe.ps1`。脚本默认使用项目 `.venv`，也可通过 `-PythonPath` 指定其他 Python。生成的 `dist/录播助手` 是完整发行目录，需保留其中全部文件；详细说明见 [打包文档](docs/PACKAGING.md)。仓库不包含 EXE、FFmpeg 二进制或语音模型权重。

## 文件位置

成片和 SRT 默认放在原录播目录。逐段烧录结果、合并文件和识别用 WAV 位于 `%LOCALAPPDATA%\LuboStudio\work`；第二阶段成功后会按“保留中间文件”设置清理。设置保存在 `%APPDATA%\LuboStudio\settings.json`。可通过 `LUBO_WORK_DIR` 更改工作目录。

## 许可证

本仓库原创源码采用 [MIT 许可证](LICENSE)。FFmpeg、FunASR、PySide6 等第三方依赖各自适用其许可证。若分发包含 FFmpeg 的 EXE，请核对所选 FFmpeg 构建的许可和源代码提供要求，参见 [FFmpeg 官方说明](https://ffmpeg.org/legal.html)及[本项目打包说明](docs/PACKAGING.md)。
