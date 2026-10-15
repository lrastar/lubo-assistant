# Windows EXE 打包

本机当前已验证入口为 [录播助手-重构升级版-v2.exe](G:/录播助手/录播助手-重构升级版-v2.exe)。下方示例输出名如已存在，应选择未占用的新名称；完整验证记录和模型比较见 [重构验证记录](REFACTOR_VALIDATION.md)。

## 干净源码构建

需要 Windows x64 和完整的 Python 3.10 环境。发行用 onedir 目录不是完整源码构建环境；它可能没有 PyInstaller 收集阶段所需的开发模块。先在项目目录准备依赖，再构建：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-quality.txt
.\build_exe.ps1 -PythonPath .\.venv\Scripts\python.exe
```

`requirements-quality.txt` 为 Qwen3-ASR、Qwen3 ForcedAligner 和中文分词补充 Transformers、Accelerate、psutil 与 jieba；它不安装或替换 Torch，主程序 Torch/torchaudio 版本由 `requirements.txt` 固定。`requirements-firered.txt` 属于 FireRed 的独立 Python 运行环境，包含单独的 CPU Torch 版本，不要把它安装到打包环境；使用 `scripts/setup_firered.ps1` 安装该环境和固定权重。

`.spec` 显式收集 Transformers 的 Qwen3-ASR 模型模块、Jieba 模块和离线词典，并把 `scripts/firered_worker.py` 与 `scripts/firered_runtime.py` 放到发行目录的 `quality_support`。`build_exe.ps1` 会在构建后确认这些文件与关键 PYZ 模块存在，并启动一次无模型加载的 worker 参数检查。

完整干净构建会替换项目内 `dist\录播助手` 和 `build`。请只在这些构建输出可安全重建时运行。构建结果位于 `dist\录播助手`，不要只复制 EXE；运行时依赖和 `quality_support` 都在同一目录。

## 基于现有 onedir 库的独立验证入口

当发行库已经包含匹配 Python 字节码版本的 PyInstaller 依赖，但缺少完整源码构建环境时，可用 `scripts/build_subtitle_update.py` 生成一个新的 EXE 入口。它会把当前源码中的全部 `lubo` 模块和 `main.py` 编译进新入口，逐项校验未改动的归档依赖和 PYZ 成员，并在 `quality_support` 中只添加缺失的 FireRed 脚本。遇到同名但内容不同的脚本时会停止，不会覆盖。

```powershell
$base = 'G:\录播助手\录播助手.exe'
$output = 'G:\录播助手\录播助手-重构验证版-v2.exe'
python .\scripts\build_subtitle_update.py `
  --base $base --output $output --project-modules --main-script .\main.py `
  --protect-exe 'G:\录播助手\录播助手.exe' `
  --protect-exe 'G:\录播助手\录播助手_新UI.exe' `
  --protect-exe 'G:\录播助手\录播助手-字幕修复版.exe' `
  --protect-exe 'G:\录播助手\录播助手-重构验证版.exe'
python .\scripts\verify_refactor_release.py `
  --app-dir 'G:\录播助手' --exe $output --base-exe $base `
  --source-root . `
  --report 'G:\录播助手\refactor-validation\packaging-v2.json' `
  --protected-exe 'G:\录播助手\录播助手.exe' `
  --protected-exe 'G:\录播助手\录播助手_新UI.exe' `
  --protected-exe 'G:\录播助手\录播助手-字幕修复版.exe' `
  --protected-exe 'G:\录播助手\录播助手-重构验证版.exe' `
  --smoke-worker-entry
```

输出名必须不存在；已占用时改用下一个空闲名称。这个入口复用同一目录里的 Qt、Torch、FunASR、Transformers 和 FFmpeg 文件，因此它是对现有发行库的增量验证，不等同于干净源码重建。新入口及 FireRed sidecar 均独立于原有 EXE 文件；共享目录中的既有依赖文件会逐项验证未变。

可用 `scripts/verify_refactor_gui_startup.py --exe $output --report 'G:\录播助手\refactor-validation\gui-v2-startup.json'` 在临时 APPDATA、LOCALAPPDATA 和 offscreen Qt 下短暂启动新 EXE 主界面；该检查不覆盖真实用户设置，也不加载 ASR 权重。它用于确认打包 GUI 能进入事件循环，不等同于桌面可见呈现或模型识别。

构建后的目录检查还不代表识别模型工作正常。完成实际父子进程、取消/恢复、GUI 和字幕两阶段验证后，记录到 [重构验证记录](REFACTOR_VALIDATION.md)。长录播效果要用实际数小时录播评估；短音频 smoke test 不能替代该评估。

## FFmpeg 和发行文件

在 PowerShell 中进入项目目录，运行：

```powershell
.\build_exe.ps1
```

默认使用项目 `.venv\Scripts\python.exe`，没有项目虚拟环境时使用 PATH 中的
`python.exe`。缺少 PyInstaller 时脚本会安装 `pyinstaller>=6.15,<7`。
如果解释器恰好是 Python 3.10.0，脚本会在 `build` 目录下载并校验
官方 Python 3.10.11 嵌入式运行时，仅用于打包，继续复用原环境的依赖。
也可通过 `-PythonPath` 指定其他已安装项目依赖的 Python。脚本从 PATH 查找 FFmpeg / FFprobe，并解析 WinGet
符号链接后复制真实 EXE；可通过 `-FfmpegPath`、`-FfprobePath`、
`-FfmpegLicensePath` 指定其他文件。

输出为 `dist\录播助手\录播助手.exe`。请保留整个 `录播助手` 文件夹，
其中包含 Python/Qt/Torch 运行库、`bin\ffmpeg.exe`、`bin\ffprobe.exe`、
`quality_support` 中的 FireRed worker 脚本和 `licenses\ffmpeg` 中的许可文件。FunASR 的 Python 源文件也必须保留：
它的模型注册器会在运行时读取类的源码。语音模型权重不随程序打包，首次识别
需要联网下载；之后使用用户目录中的模型缓存。

若所选 FFmpeg 构建启用了 GPL 组件，分发时需要履行相应许可义务。
请先阅读 [FFmpeg 许可说明](https://ffmpeg.org/legal.html)，并提供与所分发
二进制对应的源代码及构建信息。
