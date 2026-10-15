# 录播助手

Windows 本地桌面软件，处理录播姬断流产生的多个视频：配对弹幕 XML 和封面、按手动顺序拼接、识别语音生成可修改的 SRT，再把字幕烤入成片。使用 PySide6，不需要网页服务。

## 使用流程

1. 导入视频、弹幕 XML 和 JPG/PNG/WebP 封面。上方“视频分段”页签可拖动排序；“导入的资源”页签可检查配对关系，必要时选择目标视频手动关联。缺少 XML 或封面仍可继续。手动选择 `.烧录弹幕.GPU.mp4` 等已处理视频也能导入；添加文件夹时若有原片则优先选择原片，只有成片时也会导入。
2. 确认输出目录和编码设置，点击“① 生成字幕”。程序烧录弹幕、拼接分段并生成 SRT。损坏的视频会在处理前提示；音视频参数不同的分段会统一编码。
3. 修改并保存 SRT，点击“② 烤入成片”。成片和 SRT 默认放在第一段视频的目录。程序不会修改原始视频、XML 或封面。

“识别”页可选择本地高精度模式或原有快捷模式。高精度模式默认使用 Qwen3-ASR 1.7B，也可选择已安装的 FireRedASR2-AED；独立的 Qwen 强制对齐模型确定时间。
本机重构升级版已按准确率优先选择 FireRed，Qwen 可在同一页切回。固定 256 段上 FireRed 字错率为 4.53%，Qwen 为 5.02%；改善区间仍跨零，尚不能断言在所有录播上更准确。源码全新配置和未指定模型的旧配置继续使用 Qwen，避免在未安装 FireRed 隔离环境的机器上启动失败。
长录播按停顿切块并保留前后文；长句优先在中文词语之间换条。它分阶段在 CPU 上运行，适合显存较小、愿意等待更高识别质量的场景。
Qwen 的“高精度词汇提示”可填写主播名、角色名和专业术语；提示不是强制替换规则，仍需校对。FireRed 不支持词汇提示或指定语言，选择它时相关控件会禁用，原有 Qwen 偏好仍保留。
Qwen 及对齐模型首次下载约 6 GB，之后可离线识别。FireRed 使用独立 Python 环境，额外识别权重约 4.73 GB，通过 `scripts/setup_firered.ps1` 安装；本机现有安装已完成。识别用音频会按视频时间戳补偿间隙，避免剪辑点之后的字幕错位。
升级后若要重新识别已有字幕，先备份需要保留的手改 SRT，在语音识别设置中取消
“已有同名 .srt 时跳过识别（便于手动修改字幕后重烧）”，再点击“① 生成字幕”。
旧 SRT 和已经烤入字幕的成片不会随程序升级自动修正。

评测方法、已知结果和当前验证边界见 [高精度验证记录](docs/ASR_BENCHMARK.md)。未取得同素材的剪映结果前，不声称已达到或超过剪映。

## 从源码运行

需要 Windows、Python 3.10 和 FFmpeg/FFprobe。先安装 FFmpeg，并确保 `ffmpeg.exe`、`ffprobe.exe` 在 PATH 中；语音识别首次运行需要联网下载模型。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-quality.txt
.\.venv\Scripts\python.exe main.py
```

以后可双击 `启动.bat`。NVIDIA 显卡可选择 NVENC 编码；没有可用显卡时可选择 x264。高精度识别分阶段使用 CPU FP32，快捷模式使用 [FunASR](https://github.com/modelscope/FunASR) 的中文 Paraformer、FSMN-VAD 和标点模型，可选择 CUDA 或 CPU。背景音乐可能影响识别，建议烤录前校对 SRT。

## 构建 EXE

安装依赖并准备 FFmpeg 后，在 PowerShell 运行 `./build_exe.ps1`。脚本默认使用项目 `.venv`，也可通过 `-PythonPath` 指定其他 Python。生成的 `dist/录播助手` 是完整发行目录，需保留其中全部文件；详细说明见 [打包文档](docs/PACKAGING.md)。仓库不包含 EXE、FFmpeg 二进制或语音模型权重。

本轮源码、EXE 增量入口和实测边界见 [重构验证记录](docs/REFACTOR_VALIDATION.md)。增量验证入口复用现有发行目录，不能替代完整干净源码构建。

## 文件位置

成片和 SRT 默认放在原录播目录。逐段烧录结果、合并文件和识别用 WAV 位于 `%LOCALAPPDATA%\LuboStudio\work`；第二阶段成功后会按“保留中间文件”设置清理。高精度识别和对齐逐块保存检查点，中断后重试可复用已完成阶段；修改音频、模型或识别参数会使用新的任务指纹。设置保存在 `%APPDATA%\LuboStudio\settings.json`。可通过 `LUBO_WORK_DIR` 更改工作目录。

## 许可证

本仓库原创源码采用 [MIT 许可证](LICENSE)。FFmpeg、FunASR、PySide6 等第三方依赖各自适用其许可证。若分发包含 FFmpeg 的 EXE，请核对所选 FFmpeg 构建的许可和源代码提供要求，参见 [FFmpeg 官方说明](https://ffmpeg.org/legal.html)及[本项目打包说明](docs/PACKAGING.md)。
