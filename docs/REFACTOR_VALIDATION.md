# 重构验证记录（2026-10-14）

## 2026-10-15：恢复简洁界面

主窗口恢复初始版本的原生控件、分段列表、设置/日志分区和底部操作栏，移除顶部流程导航及主题切换；保留 FireRed/Qwen 模型选择、能力限制、任务控制器和检查点恢复。旧主题设置仍可读取，但不再改变界面。

当前本机入口为 [录播助手-简洁界面版.exe](G:/录播助手/录播助手-简洁界面版.exe)，SHA-256 `a14e03b90bb4e22ea3b847ac2d25bd8262749359585f58dfae8063ca62eb51d8`，继续使用同目录的运行库及模型。78 项回归测试通过；源码 GUI 的生成字幕/手改字幕/烧录流程通过，打包依赖检查、worker 入口和隔离 GUI 启动检查通过。使用真实设置的临时副本确认 FireRed 仍选中，不支持的语言与词汇控件禁用，真实设置保持原字节。界面变更未重跑模型准确率评测。

报告位于本机 `refactor-validation/simple-ui-two-stage.json`、`simple-ui-release.json`、`simple-ui-startup.json`，界面截图为 `simple-ui-model.png`。下文为各次构建的历史验证记录；旧测试版程序已于 2026-10-15 清理到回收站。

这份记录区分源码测试、打包检查和真实模型运行。短音频可确认入口、协议和恢复逻辑；它不能说明 4–6 小时录播的识别准确率、内存稳定性或处理时长。

## 已完成的源码检查

GUI 生命周期与模型能力测试在 Windows 系统 Python 3.10.0、PySide6 offscreen 环境运行；临时加入轻量测试环境中的纯 Python 依赖，没有写入真实用户设置。

| 检查 | 结果 | 覆盖内容 |
| --- | --- | --- |
| `tests/test_job_controller.py` | 5 项通过 | 工作线程取消后保持存活至线程退出、连续取消回收、退出等待、Qwen/FireRed 参数能力、任务设置与分段对象快照 |
| `tests/test_firered_report_io.py` | 3 项通过 | Windows 临时读取锁的原子发布重试、持久锁有界失败、其他 IO 错误立即传播 |
| `tests/test_artifact_store.py` | 3 项通过 | 模型指纹变化、手改字幕、来源变化、清理边界，以及无字幕哈希的旧来源记录在显式复用时保留 |
| `tests/test_config_schema.py` | 2 项通过 | 旧设置迁移、非法值恢复和 schema 版本保存 |
| `scripts/verify_gui_two_stage.py` | 通过 | offscreen GUI 对自动生成的 1 秒静音视频执行生成字幕与烧录两阶段；手工写入的 SRT 保持原文，最终视频可探测且时长为 1 秒 |
| `scripts/verify_refactor_gui_startup.py` | v2 通过 | 新 EXE v2 的 offscreen GUI 主循环运行 5 秒后仍存活；使用临时 APPDATA/LOCALAPPDATA，没有创建设置文件 |

最终完整测试套件在系统 Python 3.10.0 + PySide6 offscreen 环境中加入轻量测试依赖后，78 项全部通过，没有跳过，包括旧设置显式选择 FireRed 的迁移、安装器清单与父进程指纹协议回归。日志：[all-tests-release-v2.log](G:/录播助手/refactor-validation/all-tests-release-v2.log)。修复后的源码 GUI 两阶段脚本再次通过：[gui-source-v3-two-stage.json](G:/录播助手/refactor-validation/gui-source-v3-two-stage.json)。

重复执行 GUI 测试暴露了原生线程回收竞争：`QThread.finished` 到达 GUI 时，工作线程的 deferred QObject 删除可能尚未完成；释放最后一个 Python worker 引用会触发原生 abort。新增 25 次连续取消回归在修复前复现崩溃，控制器现在线程 finished 槽中先 `thread.wait()` 再释放引用。修复后在 20 个独立进程中运行 GUI 五项测试全部成功，覆盖 500 次连续取消及额外关闭窗口场景。[修复前回归日志](G:/录播助手/refactor-validation/qt-regression-red.log)、[修复后压力测试](G:/录播助手/refactor-validation/qt-fixed-stress.json)。

旧 SRT 回归用临时视频路径和手写 SRT：来源记录存在且分段相同，但没有 `srt_sha256`；任务选择 FireRed、开启显式复用并启用严格来源检查。流水线保留了 SRT 字节内容并将状态识别为 `legacy`。此项验证不调用 ASR 模型。

GUI 两阶段脚本直接导入源码中的 `MainWindow`，在 PySide6 offscreen 下运行；它不是 EXE 测试。脚本把 APPDATA、LOCALAPPDATA、工作目录、设置和媒体都放在系统临时目录，不改真实设置或录播文件。测试片段没有音轨，所以此源码 smoke test 验证 GUI、控制器、FFmpeg 与手改 SRT 流程，不验证任何识别模型。截图显示的是源码 GUI，不是发行版窗口。

新 EXE v2 另用 `QT_QPA_PLATFORM=offscreen` 启动，5 秒内保持运行并由测试结束进程；APPDATA、LOCALAPPDATA、工作目录、HF 和 Torch 缓存均指向一次性临时目录。该步骤验证打包 GUI 能构造并进入事件循环，不验证桌面窗口的可见呈现，也不代替真实模型运行。源码两阶段报告、v2 EXE GUI 启动报告及截图均保存在独立目录：[源码两阶段 JSON](G:/录播助手/refactor-validation/gui-source-offscreen.json)、[v2 EXE GUI 启动 JSON](G:/录播助手/refactor-validation/gui-v2-startup.json)、[源码 GUI 截图](G:/录播助手/refactor-validation/gui-source-offscreen.png)。

## 打包实现与验证边界

完整源码打包配置现在显式收集 Transformers 的 Qwen3-ASR/ForcedAligner 模块、Jieba 词典和两个 FireRed worker 支持脚本。构建脚本会检查必要归档成员、词典、支持脚本，并启动一次无权重加载的 worker CLI 检查。

`scripts/build_subtitle_update.py` 可基于现有 onedir 库生成新的入口，并逐项验证未修改的归档依赖。它只在目标不存在时添加 FireRed sidecar；遇到同名但内容不同的文件会停止。`scripts/verify_refactor_release.py` 检查 PYZ 模块、词典、sidecar、可选的基础归档依赖一致性和 worker CLI。

spec、Python 打包脚本和 PowerShell 构建脚本均通过语法解析。已用现有 onedir 发行库生成独立入口 v1：[录播助手-重构验证版.exe](G:/录播助手/录播助手-重构验证版.exe)，SHA-256 为 `b8d05cfcdd983a63746981a16e143489482f35fa778febc329458432943fb086`。源入口和另外两款旧 EXE 构建前后都保持 SHA-256 `6cf5a86ba14d5d5ef5c81af80a61c3c1c8d4615aacd64fff9bdad33e56c69fc2`。后续 FireRed 多次运行暴露了 worker report 路径复用问题，v1 保留用于诊断，不建议用于重复任务或比较。

增量构建 v1 与 v2 均重新编译 25 个项目模块，并保留 12,096 个其他 PYZ 模块原始字节。v2 的发行验证器确认 12,121 个 PYZ 模块、Qwen3-ASR 模块、Jieba 词典（5,071,852 字节）、两个 FireRed sidecar 的源码哈希及未改动归档依赖；它也实际启动 v2 的 `--quality-worker --help` 并成功退出。当前推荐验证入口为 [录播助手-重构验证版-v2.exe](G:/录播助手/录播助手-重构验证版-v2.exe)，SHA-256 `cbf17897a7215c2dbb7a3023712764da93fbff106ff9182448e0468a6cc2f424`。v2 构建时保护了三款原入口和 v1，哈希均未变。

这两个入口均复用现有 onedir 库，属于增量验证，不是干净源码构建。已检查完整构建环境：系统仅有 Python 3.10.0；项目没有完整 `.venv`，只有无 Qt/Torch 的轻量测试环境；系统 Python 有 PySide6 6.12.0、PyInstaller 6.18.0，但缺 Torch、Transformers、FunASR 和 ModelScope。项目基础依赖 `requirements.txt` 固定 Torch 2.14.0、torchaudio 2.11.0；补充依赖 `requirements-quality.txt` 固定 Transformers 5.17.0、Accelerate 1.15.0、psutil 7.0.0 和 jieba 0.42.1，不安装或替换 Torch。FireRed 的 CPU Torch 2.9.1+cpu 与 torchaudio 2.9.1+cpu 单独列在 `requirements-firered.txt`，供外置 worker 环境使用，不属于主程序干净构建依赖。pip wheel 缓存只有少量小包，没有 Torch/torchaudio。G 盘尚有 73.8 GB，F 盘 178.8 GB，可用空间充足；但下载并安装完整依赖、再做 PyInstaller 收集会与正在运行的 CPU 模型评测争用资源。本轮没有开始完整源码构建，避免影响 256 条识别比较；因此干净源码构建尚未验证。`torchgen` 缺失是现有发行目录不能直接作为完整构建环境的原因，见 [打包说明](PACKAGING.md)。

完整 v2 增量发行验包 JSON：[packaging-v2.json](G:/录播助手/refactor-validation/packaging-v2.json)。v1 验包记录仍保留于 [packaging-incremental.json](G:/录播助手/refactor-validation/packaging-incremental.json)。

FireRed SentencePiece Unicode 路径兼容层与 Windows HANDLE 签名修正是在首次增量构建后落入外置 `quality_support` sidecar 的；未重新编译 EXE。sidecar 已逐字节核对源码并重跑验包，EXE SHA-256 仍为 `b8d05cfcdd983a63746981a16e143489482f35fa778febc329458432943fb086`。当前发行 sidecar：`firered_worker.py` SHA-256 `e5e14af5fc994848d0c1c23a9480993b106d235e4e0cd12b624ab492fa6aa6b2`，`firered_runtime.py` SHA-256 `27afb9606a9f7f720a9eee8d2111f46181afd046ae5f33a7623360e82577415e`。

## 真实识别验证

`scripts/check_quality_flow.py` 已可通过 `--model qwen3` 或 `--model firered_asr2_aed` 选择新 EXE 的真实 worker 路径；它按结果文件名把 checkpoint 放在独立目录，同一音频、同一结果路径再次运行可检查恢复。

FireRed 固定权重与代码 revision 已校验。源 worker 在真实 G 盘目录完成一个 7.48 秒音频的 CPU smoke：识别和 FireRed CTC 时间戳均成功，文本非空；总耗时 58.081 秒（含 6.072 秒模型文件校验和 16.015 秒模型加载），峰值工作集 9.74 GB。第一次运行因 SentencePiece 无法从中文路径加载 BPE 模型而失败，之后加入 worker 内的 bytes 加载兼容后通过。此记录验证独立 worker 路径，不等于 EXE 父子进程或应用内 Qwen forced alignment。详见[首次失败报告](G:/录播助手/refactor-validation/firered-worker-smoke-unicode-path-failure.json)和[修复后 worker smoke](G:/录播助手/refactor-validation/firered-worker-smoke.json)。

随后，新 EXE v1 的首次父子进程集成 smoke 对同一 7.48 秒音频成功完成，报告包含 42 个 Qwen ForcedAligner 字词时间戳和 3 条 cue，总流程约 82.0 秒。FireRed 子进程单独记录模型加载 13.252 秒、ASR 子进程耗时 55.207 秒、峰值工作集 9.78 GB；峰值数值来自 FireRed worker 报告，不代表包含 Qwen 对齐进程的整套程序峰值。详见[EXE 外层结果](G:/录播助手/refactor-validation/firered-exe-smoke.json)与[FireRed 内层报告](G:/录播助手/refactor-validation/firered-exe-smoke-inner.json)。

后续复用 v1 做 256 条运行时，adapter 在新任务写出当前 report 前读到了上一次 smoke 的 `firered-report.json`。该污染批次已由 19/256 处停止，原始文件保留；只有 18 个当前样本完成，因此整批不用于打分。core 已修为每次尝试使用独立 UUID 子目录，并通过连续两次复用同一外层日志目录的回归测试；v2 已从修复后的快照生成。v2-01 smoke 在识别输出前被并行取消测试打断；runtime 已转到新目录 v2-02，重新运行 fresh smoke 后再启动 256 条识别/对齐。报告返回后再补记实际结果。[中断批次诊断记录](G:/录播助手/refactor-validation/firered-stale-report-carryover/attempt-metadata.json)。

FireRed v2 外置 worker 进程树取消检查已通过：任务启动约 0.806 秒后观察到 worker 进程，2.845 秒发出取消；外层 EXE、venv launcher、uv Python worker 和 conhost 均在总计 3.911 秒内退出，未留存子进程。外层结果为 `complete=false`、`ok=false`、`sample_count=0`，没有字幕样本或半成品；该检查验证取消和进程清理，不代表完成 FireRed 推理。[FireRed v2 取消证据 JSON](G:/录播助手/refactor-validation/firered-cancel-v2.json)。

Qwen v1 的短音频父子进程取消/恢复实测已完成，仍适用于当前 GUI/controller 和恢复代码：72.369 秒测试音频切成三块；首块持久化提交后取消，取消任务没有成功结果且只留下 `chunk-00000`。续跑只识别 `chunk-00001` 和 `chunk-00002`，随后对齐三块并输出 18 条 cue。再执行缓存命中任务耗时 0.412 秒，stage 哈希和修改时间未变，且没有载入 ASR 或对齐模型。[Qwen v1 取消恢复证据 JSON](G:/录播助手/refactor-validation/qwen-cancel-resume-v1.json)。该短样本证明检查点恢复路径，不替代长录播准确率评估。

v2-03 完整评测在第 75 个报告写入时遇到 Windows `WinError 5`：adapter 短暂读取 report 与 worker 原子替换发生竞争。该不完整批次不参与评分。FireRed writer 现在仅对 Windows 错误码 5/32/33 重试，间隔 50 ms、截止 2 秒，保留原报告；其他 IO 错误及持久锁仍明确失败。新增回归先失败、修复后通过，原始失败日志保留在 `asr_benchmark/refactor-firered/attempt-20261014-v2-03`。

v3 从修复后的源码增量生成，SHA-256 `fb442802a558d3da5c073f05324b2d422004d354423de80cf8c5b9523f8fa8c6`。三款原 EXE、v1、v2 均保持原哈希；旧 FireRed sidecar 保存于 `refactor-validation/quality-support-before-v3`。当前 worker sidecar SHA-256 为 `f7514bf486aef8d9c3503b369bfdfe72189ed750ce95e69153b03f29c62e5a70`。v3 完成归档/源码 sidecar 验证、worker CLI 与 5 秒隔离 GUI 启动：[packaging-v3.json](G:/录播助手/refactor-validation/packaging-v3.json)、[gui-v3-startup.json](G:/录播助手/refactor-validation/gui-v3-startup.json)。它仍属于增量构建。

v3-01 全量评测已完成：256/256 识别和对齐成功，1194.964 秒音频，1589.841 秒处理，430 条字幕。原文和最终字幕 CER 均为 4.5326%（240/5295），Qwen 基线为 5.0236%（266/5295）。256 段文字全部保留，字幕时间范围和起点顺序检查没有问题。51 段改善、169 段相同、36 段变差；分组配对 bootstrap 改善区间为 -0.0606 至 1.0397 个百分点，不能宣称普遍显著提升。完整结果与限制见 [模型比较记录](ASR_BENCHMARK.md)。运行启用 `HF_HUB_OFFLINE=1` 和 `TRANSFORMERS_OFFLINE=1`，任务仅包含音频路径和 ID，没有参考文本。

中间入口 `录播助手-重构升级版.exe`，SHA-256 `e5da039b13b5d4c1fbde4eda130f5200bd0d704449aed2d0d1bac046de6a45cb`，完成归档、CLI 和隔离 GUI 启动验证，但真实 recording 父进程检查发现安装器清单与 registry 的 `code_repo` 不一致：清单使用完整 GitHub URL，registry 使用仓库简称。直接 benchmark worker 绕过了该父进程指纹检查，因此此前完整识别比较有效，却不代表 recording 入口已通过。中间入口保留用于诊断，不推荐使用。新增协议回归先失败，registry 现统一为安装器 URL；没有改权重或代码 revision。

最终入口 [录播助手-重构升级版-v2.exe](G:/录播助手/录播助手-重构升级版-v2.exe)，SHA-256 `773e1dd973504be16fd62eec1b78fe724c6572e8c74c64cb16b81650c163fd5d`，完成归档、worker CLI 和隔离 GUI 启动：[验包](G:/录播助手/refactor-validation/packaging-release-v2.json)、[GUI 启动](G:/录播助手/refactor-validation/gui-release-v2-startup.json)。它仍是复用 onedir 的增量发行；25 个项目模块与 v3 做 code object 比较，仅 config 的迁移提示、asr_profiles 的仓库 URL 改变，识别、对齐、控制器实现不变。`segments` 的 marshal 字节可以因 frozenset 序列化顺序不同而不同，code object 相等。[代码等价检查](G:/录播助手/refactor-validation/v3-release-v2-equivalence.json)。

依据用户准确率优先的选择，本机新版本已选择本组错误较少的 FireRed，并保留 Qwen 回退；这一部署选择不等于统计优势已确定。本机真实设置仅添加 `asr.quality_model=firered_asr2_aed`，其他 JSON 值不变，原设置逐字节备份在 `refactor-validation/settings-before-model-upgrade-5e1c9988f759.json`。[选择记录](G:/录播助手/refactor-validation/selected-model.json)。复制实际设置到临时目录加载源码 GUI，确认选中 FireRed、禁用不支持的语言和词汇控件，真实设置字节不变：[GUI 选择检查](G:/录播助手/refactor-validation/selected-model-gui.json)。源码全新配置和未指定 profile 的旧配置仍使用 Qwen，避免未安装 FireRed 的机器启动失败。

现有真实录播九个 20 秒片段也完成 FireRed 离线识别/对齐，9/9 成功，298.958 秒，44 条字幕，九段归一化文字全部保留，字幕时间范围及顺序无错误。没有人工校对参考，不能计算真实录播 CER。[检查报告](G:/录播助手/asr_benchmark/refactor-firered/recording-v3-01/validation-audit.json)。

最终升级版 v2 的实际 production recording 父子进程取消/恢复检查通过：同一 72.369 秒、三块音频，在 `chunk-00000` 持久化后取消，约 0.5 秒回收 worker；取消不交付成功字幕。续跑仅识别 `chunk-00001` 和 `chunk-00002`，首块识别缓存 SHA-256 与修改时间均未变；三块对齐完成，输出 12 条 cue。再跑同一任务约 0.948 秒命中完整缓存，六个识别/对齐文件的哈希、修改时间未变，未加载模型。[取消证据](G:/录播助手/asr_benchmark/refactor-validation/firered-release-cancel-resume/cancel-evidence.json)、[续跑核对](G:/录播助手/asr_benchmark/refactor-validation/firered-release-cancel-resume/resume-check.json)、[缓存核对](G:/录播助手/asr_benchmark/refactor-validation/firered-release-cancel-resume/cache-check.json)。该测试使用最终 EXE、固定真实模型和 production `QualityTranscriber`，补上了直接 benchmark worker 无法验证的父进程入口。

4–6 小时录播的识别准确率、长期运行稳定性、分段边界和总处理时长均未验证。获得同素材人工校对稿之前，也不比较或宣称优于剪映。
