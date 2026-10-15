param(
    [string]$PythonPath = '',
    [string]$FfmpegPath = '',
    [string]$FfprobePath = '',
    [string]$FfmpegLicensePath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw '此脚本只支持 Windows。'
}

function Resolve-ActualFile([string]$Path) {
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    for ($depth = 0; $depth -lt 8 -and $item.LinkType -eq 'SymbolicLink'; $depth++) {
        $target = $item.Target
        if ($target -is [array]) { $target = $target[0] }
        if (-not [IO.Path]::IsPathRooted($target)) {
            $target = Join-Path $item.DirectoryName $target
        }
        $item = Get-Item -LiteralPath $target -ErrorAction Stop
    }
    if ($item.LinkType -eq 'SymbolicLink' -or $item.PSIsContainer) {
        throw "无法解析实际文件：$Path"
    }
    return $item.FullName
}

function Resolve-Tool([string]$Name, [string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) {
        $command = Get-Command "$Name.exe" -CommandType Application -ErrorAction Stop
        $Path = $command.Source
    }
    return Resolve-ActualFile $Path
}

$projectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $localPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $localPython -PathType Leaf) {
        $PythonPath = $localPython
    }
    else {
        $PythonPath = (Get-Command 'python.exe' -CommandType Application -ErrorAction Stop).Source
    }
}
$packagePython = Resolve-ActualFile $PythonPath
$python = $packagePython
$spec = Join-Path $projectRoot '录播助手.spec'
$distRoot = Join-Path $projectRoot 'dist'
$workRoot = Join-Path $projectRoot 'build'
$outputDir = Join-Path $distRoot '录播助手'

if (-not (Test-Path -LiteralPath $spec -PathType Leaf)) {
    throw "找不到打包配置：$spec"
}

# PyInstaller 的 --noconfirm 会替换旧输出，先确认目标确实在本项目内且不是联接。
foreach ($target in @($distRoot, $workRoot, $outputDir)) {
    $absolute = [IO.Path]::GetFullPath($target)
    if (-not $absolute.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase)) {
        throw "打包目录不在项目内：$absolute"
    }
    if (Test-Path -LiteralPath $target) {
        $item = Get-Item -LiteralPath $target -Force
        if ($item.LinkType) { throw "打包目录是符号链接或联接，拒绝覆盖：$target" }
    }
}

# 现有项目虚拟环境建立于 Python 3.10.0；该版本的 dis 字节码错误使
# PyInstaller 官方明确不支持它。只下载官方 3.10.11 嵌入式解释器作为
# 构建运行时，继续使用原 venv 的 cp310 site-packages，不改系统 Python。
$pythonVersion = & $python -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'
if ($LASTEXITCODE -ne 0) { throw "无法运行 Python：$python" }
if ($pythonVersion -eq '3.10.0') {
    $embedDir = Join-Path $workRoot 'python-3.10.11-embed-amd64'
    $embedZip = Join-Path $workRoot 'python-3.10.11-embed-amd64.zip'
    $embedPython = Join-Path $embedDir 'python.exe'
    $expectedMd5 = 'f1c0538b060e03cbb697ab3581cb73bc'
    if (-not (Test-Path -LiteralPath $embedPython -PathType Leaf)) {
        New-Item -ItemType Directory -Path $workRoot -Force | Out-Null
        if (-not (Test-Path -LiteralPath $embedZip -PathType Leaf)) {
            Write-Host '下载官方 Python 3.10.11 构建运行时…'
            Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip' -OutFile $embedZip
        }
        $actualMd5 = (Get-FileHash -LiteralPath $embedZip -Algorithm MD5).Hash.ToLowerInvariant()
        if ($actualMd5 -ne $expectedMd5) {
            throw "Python 3.10.11 下载校验失败：$embedZip"
        }
        Expand-Archive -LiteralPath $embedZip -DestinationPath $embedDir -Force
    }
    $sitePackages = & $packagePython -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])'
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $sitePackages -PathType Container)) {
        throw '找不到原虚拟环境的 site-packages。'
    }
    @('python310.zip', '.', $sitePackages, 'import site') |
        Set-Content -LiteralPath (Join-Path $embedDir 'python310._pth') -Encoding Ascii
    $python = Resolve-ActualFile $embedPython
    $pythonVersion = & $python -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'
    if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne '3.10.11') {
        throw '官方 Python 3.10.11 构建运行时无法启动。'
    }
}

$ffmpeg = Resolve-Tool 'ffmpeg' $FfmpegPath
$ffprobe = Resolve-Tool 'ffprobe' $FfprobePath
if ([string]::IsNullOrWhiteSpace($FfmpegLicensePath)) {
    $FfmpegLicensePath = Join-Path (Split-Path -Parent (Split-Path -Parent $ffmpeg)) 'LICENSE'
}
$ffmpegLicense = Resolve-ActualFile $FfmpegLicensePath
$ffmpegReadme = Join-Path (Split-Path -Parent (Split-Path -Parent $ffmpeg)) 'README.txt'

Write-Host "Python: $python"
Write-Host "FFmpeg: $ffmpeg"
Write-Host "FFprobe: $ffprobe"
Write-Host "FFmpeg 许可文件: $ffmpegLicense"

& $python -c 'import PyInstaller'
if ($LASTEXITCODE -ne 0) {
    Write-Host '安装 PyInstaller 到项目虚拟环境…'
    & $packagePython -m pip install 'pyinstaller>=6.15,<7'
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 安装失败。' }
    & $python -c 'import PyInstaller'
    if ($LASTEXITCODE -ne 0) { throw '构建运行时仍无法导入 PyInstaller。' }
}

$env:LUBO_FFMPEG_EXE = $ffmpeg
$env:LUBO_FFPROBE_EXE = $ffprobe
$env:LUBO_FFMPEG_LICENSE = $ffmpegLicense
$env:LUBO_FFMPEG_README = if (Test-Path -LiteralPath $ffmpegReadme -PathType Leaf) { $ffmpegReadme } else { '' }

Push-Location $projectRoot
try {
    & $python -m PyInstaller --noconfirm --distpath $distRoot --workpath $workRoot $spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败，退出码 $LASTEXITCODE。" }
}
finally {
    Pop-Location
}

$exe = Join-Path $outputDir '录播助手.exe'
foreach ($required in @($exe, (Join-Path $outputDir 'bin\ffmpeg.exe'),
        (Join-Path $outputDir 'bin\ffprobe.exe'),
        (Join-Path $outputDir 'licenses\ffmpeg\LICENSE'),
        (Join-Path $outputDir 'quality_support\firered_worker.py'),
        (Join-Path $outputDir 'quality_support\firered_runtime.py'))) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "打包产物缺少文件：$required"
    }
}
& $python (Join-Path $projectRoot 'scripts\verify_refactor_release.py') `
    --app-dir $outputDir --exe $exe --source-root $projectRoot --smoke-worker-entry
if ($LASTEXITCODE -ne 0) { throw '发行目录验证失败。' }
Write-Host "打包完成：$exe"
