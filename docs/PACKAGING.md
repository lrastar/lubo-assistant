# Windows EXE 打包

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
其中包含 Python/Qt/Torch 运行库、`bin\ffmpeg.exe`、`bin\ffprobe.exe`
和 `licenses\ffmpeg` 中的许可文件。FunASR 的 Python 源文件也必须保留：
它的模型注册器会在运行时读取类的源码。语音模型权重不随程序打包，首次识别
需要联网下载；之后使用用户目录中的模型缓存。

若所选 FFmpeg 构建启用了 GPL 组件，分发时需要履行相应许可义务。
请先阅读 [FFmpeg 许可说明](https://ffmpeg.org/legal.html)，并提供与所分发
二进制对应的源代码及构建信息。
