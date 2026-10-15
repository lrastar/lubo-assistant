# 录播助手 (LuboStudio) — 设计文档

单机桌面软件（PySide6，无网页前后端）。替代既有弹幕烧录批处理流程，
把一场直播的多个断流分段处理成一个带弹幕 + 自动字幕的成品视频。

## 用户确定的流程

2026-09-24 确认：手动选择一场直播的分段并调整顺序；缺 XML 或封面继续处理，损坏段报错；音视频参数不同时自动统一；先生成 SRT 供人工修改，再单独点击烤入成片；成片和 SRT 默认放第一段的原目录；典型场次为 4–6 小时；交付 Windows EXE。

界面的两个主按钮分别执行步骤 1–3 和步骤 4。第一阶段完成后持久保留工作文件，第二阶段成功且未勾选“保留中间文件”时清理工作文件。

1. **每段烧录弹幕**：分段视频 + 同名 `.xml` 弹幕 → XML 转 ASS → NVENC 编码 → `{stem}.烧录弹幕.GPU.mp4`
2. **拼接**：把第 1 步的产物按顺序无损拼接（concat demuxer, `-c copy`）→ 工作目录中的 `{base}.合并.mp4`
3. **语音识别**：从合并视频抽 16k 单声道 wav → FunASR Paraformer（paraformer-zh + fsmn-vad + ct-punc）→ `{base}.srt`
4. **烧录字幕**：合并视频 + 字幕 ASS → NVENC 编码 + 附加封面 → `{base}.完整版.mp4`

开发验证环境：Windows 11、Python 3.10、FFmpeg 8。支持 NVIDIA NVENC，
也可使用 CPU 编码。

## 目录结构

```
main.py                 # 入口：python main.py（启动 GUI）
启动.bat                # 用 .venv\Scripts\pythonw.exe 启动 main.py
requirements.txt
README.md
lubo/
  __init__.py
  config.py             # 设置 dataclass + JSON 持久化
  ffmpeg.py             # 查找 ffmpeg/ffprobe、probe、带进度/可取消的 ffmpeg 运行器
  danmaku.py            # B站/录播姬 XML → ASS（移植自 录播机/webhook_uploader/__main__.py）
  segments.py           # 扫描分段、匹配 xml/封面、解析文件名时间、排序
  subtitles.py          # Cue、SRT 读写、字幕 ASS 生成
  asr.py                # FunASR 封装（懒加载、分块、进度、取消、释放显存）
  pipeline.py           # 四步流水线编排
  gui/
    __init__.py
    main_window.py      # 主窗口
    worker.py           # QThread 后台执行 Pipeline
```

## 模块接口（各模块必须严格遵守这些签名）

### lubo/config.py

```python
@dataclass
class DanmakuStyle:
    font: str = "Microsoft YaHei"
    font_scale: float = 1.4
    opacity: int = 40              # 0-100 可见度
    scroll_duration: float = 8.0
    static_duration: float = 5.0
    area_ratio: float = 0.35       # 弹幕占画面高度比例

@dataclass
class SubtitleStyle:
    font: str = "Microsoft YaHei"
    size_ratio: float = 0.055      # 字号 = 视频高度 * size_ratio
    outline: float = 3.0           # 以 1080p 为基准，按高度缩放
    margin_ratio: float = 0.04     # 底边距 = 高度 * margin_ratio
    primary_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    max_chars: int = 22            # 每条字幕最多字数（超出按字级时间戳拆分）

@dataclass
class EncodeOptions:
    encoder: str = "h264_nvenc"    # h264_nvenc / hevc_nvenc / libx264
    preset: str = "p5"             # nvenc: p1-p7；libx264: ultrafast..veryslow
    danmaku_cq: int = 21           # 第 1 步质量（中间文件，略高以减少二次编码损失）
    final_cq: int = 24             # 第 4 步质量
    audio_bitrate: str = "192k"    # 需要重编码音频时使用

@dataclass
class AsrOptions:
    device: str = "cuda"           # cuda / cpu（cuda 不可用时自动退回 cpu）
    model: str = "paraformer-zh"
    vad_model: str = "fsmn-vad"
    punc_model: str = "ct-punc"
    chunk_seconds: float = 120.0   # 按 VAD 静音边界切块，每块不超过该时长
    reuse_existing_srt: bool = True  # {base}.srt 已存在则跳过识别（方便手动改字幕后重烧）

@dataclass
class Steps:
    burn_danmaku: bool = True
    concat: bool = True
    asr: bool = True
    burn_subtitles: bool = True

@dataclass
class Settings:
    danmaku: DanmakuStyle
    subtitle: SubtitleStyle
    encode: EncodeOptions
    asr: AsrOptions
    steps: Steps
    output_dir: str = ""           # 空 = 第一个分段所在目录
    keep_intermediate: bool = False # 第二阶段成功后删除任务工作目录中的中间文件
    overwrite: bool = False        # False 时已存在的中间产物直接复用
    last_folder: str = ""
    window_geometry: str = ""      # base64 of QByteArray

def settings_path() -> Path        # %APPDATA%\LuboStudio\settings.json
def load_settings(path: Path | None = None) -> Settings    # 缺失/未知键/损坏文件都要容错，回退默认值
def save_settings(settings: Settings, path: Path | None = None) -> None
```

### lubo/ffmpeg.py

```python
class FfmpegError(RuntimeError): ...
class Cancelled(Exception): ...

@dataclass
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    vcodec: str
    acodec: str | None
    sample_rate: int | None
    channels: int | None
    has_audio: bool

def find_tool(name: str) -> str            # "ffmpeg"/"ffprobe"：先找 app 目录下 bin\，再找 PATH；找不到抛 FfmpegError
def probe(path: Path) -> MediaInfo
def has_encoder(name: str) -> bool         # 解析 ffmpeg -encoders，结果缓存
def run_ffmpeg(args: list[str], duration: float,
               on_progress: Callable[[float], None] | None = None,   # 0..1
               cancel: threading.Event | None = None,
               log: Callable[[str], None] | None = None,
               cwd: Path | None = None) -> None
```

`run_ffmpeg` 细节：
- 自动在前面加 `ffmpeg -hide_banner -nostdin -y -progress pipe:1 -nostats -loglevel error`（args 不含 "ffmpeg"）。
- Windows 下 `creationflags=CREATE_NO_WINDOW`，GUI 不能弹黑窗口。
- 用线程分别读 stdout（进度：out_time_us/out_time_ms/out_time/progress=end）和 stderr（保留最后 30 行），避免管道阻塞死锁。
- 取消：cancel 被置位后 terminate 进程，2 秒内未退出则 kill，抛 `Cancelled`。
- 退出码非 0 抛 `FfmpegError`，消息包含 stderr 尾部。
- 所有文本用 utf-8, errors="replace"。

**路径转义规则**：`ass=`/`subtitles=` 滤镜里的路径在 Windows 下转义极易出错（盘符冒号、中文、`'`、`[`、`,`）。
统一做法：把 ASS 文件写到一个纯 ASCII 名字的工作目录（`tempfile.mkdtemp(prefix="lubo_")`），
ffmpeg 以该目录为 `cwd` 运行，滤镜里只写 `ass=danmaku.ass`。输入/输出文件路径作为普通参数传递即可（不需要转义）。

### lubo/danmaku.py

```python
def xml_to_ass(xml_path: Path, ass_path: Path, width: int, height: int, style: DanmakuStyle) -> int  # 返回写入条数
```
- 弹幕逻辑沿用原有批处理方案中的轨道分配、mode 4/5/6、颜色、透明度与转义规则。
- 录播姬断流时 XML 可能**没有闭合**（被截断），`iterparse` 会在末尾抛 `ParseError`：必须捕获并保留已解析的弹幕。
- XML 里可能含非法控制字符：解析失败时先清洗非法 XML 字符再重试。
- 空弹幕文件 → 仍然输出合法的 ASS（只有头部），返回 0。
- 字号按分辨率缩放：原代码以 B站默认 25 号为基准，这里以 1080p 为基准，按 `height/1080` 缩放。

### lubo/segments.py

```python
VIDEO_SUFFIXES = {".flv", ".mp4", ".mkv", ".ts"}
GENERATED_MARKERS = (".烧录弹幕", ".GPU", ".合并", ".完整版", ".burned", ".part")

@dataclass
class Segment:
    video: Path
    xml: Path | None
    cover: Path | None
    start_time: datetime | None   # 从文件名解析
    size: int                     # 字节
    info: MediaInfo | None = None # probe 结果（失败为 None，并记录 error）
    error: str | None = None

def is_source_video(path: Path) -> bool
def parse_start_time(name: str) -> datetime | None
    # 支持: "【xx／录播】2026_06_07-13_23_43 标题"、录播姬默认 "录制-123-20260607-132343-123-标题"
def locate_sidecar(video: Path, suffixes: tuple[str, ...]) -> Path | None   # 移植 locate_sidecar_once + pairing_key
def make_segment(video: Path, probe_media: bool = True) -> Segment
def scan_folder(folder: Path, recursive: bool = False) -> list[Segment]   # 已排序
def sort_segments(segments: list[Segment]) -> list[Segment]   # start_time 优先，其次文件名
def default_output_base(segments: list[Segment]) -> str       # 第一个分段的 stem
```
封面后缀：`(".cover.jpg", ".jpg", ".png")`；弹幕后缀：`(".xml",)`。

### lubo/subtitles.py

```python
@dataclass
class Cue:
    start: float   # 秒
    end: float
    text: str

def write_srt(cues: list[Cue], path: Path) -> None
def read_srt(path: Path) -> list[Cue]       # 容错：BOM、\r\n、空行、逗号/点毫秒
def write_ass(cues: list[Cue], path: Path, width: int, height: int, style: SubtitleStyle) -> None
    # 底部居中（\an2），白字黑边，字号/描边/边距按分辨率缩放；文本转义 { } \ 与换行
```

### lubo/asr.py

```python
class Transcriber:
    def __init__(self, options: AsrOptions, log: Callable[[str], None] | None = None): ...
    def load(self) -> None                    # 懒加载 funasr.AutoModel；首次运行会从 ModelScope 下载模型（记录日志）
    def transcribe(self, wav_path: Path, max_chars: int,
                   on_progress: Callable[[float], None] | None = None,
                   cancel: threading.Event | None = None) -> list[Cue]
    def close(self) -> None                   # 释放模型 + torch.cuda.empty_cache()
```
- `funasr`/`torch` 只能在函数内 import（启动 GUI 不能因为它们变慢或崩溃）。
- 长音频（数小时）处理：用标准库 `wave` 按最长 120 秒的窗口读取 16 kHz/16 位/单声道 WAV，逐窗做 FSMN-VAD，避免把整场音频加载进内存；再按静音边界把音频切成
  ≤ `chunk_seconds` 的块；对每块调用 ASR+标点（带 `sentence_timestamp=True`），把时间偏移回全局时间；
  每块完成后回调进度并检查 cancel（抛 `lubo.ffmpeg.Cancelled`）。
  该路径作为快捷模式保留。默认高精度模式由 `lubo/asr_backend.py` 创建独立进程，使用带上下文的有界分块、Qwen3-ASR 与独立强制对齐；完整精度 CPU 运行、恢复重试和最终时间轴拼接分别在 `quality_worker.py` / `quality_recording.py` 中处理。
- 句子超过 `max_chars` 时，利用字级时间戳拆分成多条 Cue；去掉句尾的逗号句号等标点；过滤空文本。
- cuda 不可用时自动退回 cpu 并记录日志。

### lubo/pipeline.py

```python
@dataclass
class Job:
    segments: list[Segment]    # 已按用户顺序排列
    settings: Settings
    output_dir: Path
    output_base: str           # 输出文件名前缀

@dataclass
class PipelineCallbacks:
    log: Callable[[str], None]
    step: Callable[[str], None]              # 当前步骤描述，例如 "烧录弹幕 (2/3)"
    progress: Callable[[float], None]        # 整体进度 0..1

class Pipeline:
    def __init__(self, job: Job, callbacks: PipelineCallbacks): ...
    def run(self, cancel: threading.Event) -> list[Path]   # 返回最终产物路径列表
```

输出文件：
| 步骤 | 文件位置 |
|---|---|
| 1 | `%LOCALAPPDATA%\LuboStudio\work\<任务哈希>\{segment.stem}.烧录弹幕.GPU.mp4`（每段一个） |
| 2 | 同一任务工作目录中的 `{base}.合并.mp4`；仅执行纯拼接时直接交付到 `output_dir` |
| 3 | `output_dir\{base}.srt`（最终保留）；识别用 WAV 在任务工作目录 |
| 4 | `output_dir\{base}.完整版.mp4` |

规则：
- 所有输出先写到 `*.part.mp4`（或 `.part.srt`）再原子重命名，取消/失败不会留下看似完整的文件。
- `overwrite=False` 时已存在的产物直接复用并记录日志（中断后可以续跑）。
- 开始前 probe 所有分段。若分段**分辨率不一致**：第 1 步统一 `scale+pad` 到第一个分段的分辨率（保持比例、黑边）。
  若**音频参数不一致**（编码/采样率/声道）或没有音频：第 1 步音频统一编码为 AAC 48k 立体声；否则 `-c:a copy`。
- 第 1 步：某段没有 xml 时，照常编码（不加弹幕滤镜），保证所有分段编码参数一致，拼接不出错。
- 第 1 步输出不附加封面（避免 concat 出问题）；只映射 `0:v:0` 和 `0:a:0?`。
- 第 2 步：只有 1 个分段时跳过拼接，直接用该分段产物。concat 列表文件写到工作目录，用 `file 'path'` 格式，路径中的 `'` 转义为 `'\''`，加 `-safe 0`；`-map 0:v:0 -map 0:a? -c copy -movflags +faststart`。
- 第 3 步：`ffmpeg -i merged -map 0:a:0 -vn -ac 1 -ar 16000 -af aresample=async=1000:first_pts=0 -c:a pcm_s16le {base}.wav`；缓存记录包含抽取版本与源文件状态，旧 WAV 会重新生成；识别完成后关闭对应的识别后端。
- 第 4 步：`[0:v]ass=subs.ass[v]`；封面取第一个带封面的分段，作为 `attached_pic`（`-c:v:1 mjpeg -disposition:v:1 attached_pic`）；`-c:a copy`；`-movflags +faststart`。
- 编码参数：nvenc → `-c:v X -preset P -rc vbr -cq N -b:v 0 -pix_fmt yuv420p`；libx264 → `-c:v libx264 -preset P -crf N -pix_fmt yuv420p`。
  若选的是 nvenc 但 `has_encoder` 为 False，退回 libx264（preset 映射为 medium）并记录日志。
- 步骤开关：
  - `burn_danmaku=False`：第 2 步直接拼接原始分段（`-c copy`）；若分段参数不兼容则记录警告并按第 1 步的规则重新编码（不加弹幕）。
  - `concat=False`：第 3、4 步对每个分段各自执行，产物以各自的 stem 命名。
  - `asr=False`：第 4 步若已有 `{base}.srt` 则使用，否则跳过第 4 步。
  - `burn_subtitles=False`：只导出 srt，最终产物为合并文件。
- `keep_intermediate=False`：第二阶段全部成功后删除任务工作目录中的烧录分段、合并文件、WAV；临时 ASS 随运行结束清理。**永不删除原始录播文件、xml、封面**。
- 整体进度按时长加权：第 1 步 = 各段时长之和（权重 1.0），第 2 步 0.05，第 3 步 0.5，第 4 步 1.0。
- 工作目录（tempfile.mkdtemp）在 finally 中清理。

### lubo/gui

- `main_window.py`：`class MainWindow(QMainWindow)`；`def run_app() -> int`（`main.py` 调用）。
- 分段列表：`QTreeWidget`，列 = 序号 / 文件名 / 开始时间 / 时长 / 分辨率 / 弹幕 / 封面 / 大小；支持拖拽排序
  （InternalMove）、从资源管理器拖入文件或文件夹、按钮：添加文件夹 / 添加文件 / 上移 / 下移 / 移除 / 清空 / 按时间排序。
  probe 在后台线程进行（大文件夹不能卡界面）。
- 设置区（可折叠分组或 Tab）：步骤开关、编码参数、弹幕样式、字幕样式、ASR 设备、输出目录与文件名前缀、保留中间文件、覆盖已有文件。
- 底部：开始 / 取消按钮、整体进度条、当前步骤文字、日志窗口（只读、自动滚动）。
- `worker.py`：`class PipelineWorker(QObject)` 放入 `QThread`；信号 `log(str)`、`step(str)`、`progress(float)`、`finished(list)`、`failed(str)`、`cancelled()`；
  所有跨线程 UI 更新只能走信号。
- 关闭窗口时若有任务在跑：询问确认，确认后取消并等待线程结束；保存设置和窗口几何。
- 界面文字全部中文。
