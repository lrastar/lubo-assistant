param(
    [string]$PythonExe = "",
    [string]$RuntimeRoot = "",
    [switch]$SkipModelDownload
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$installedRoot = Split-Path -Parent (Split-Path -Parent $projectRoot)
$appRoot = if (Test-Path -LiteralPath (Join-Path $installedRoot "asr_runtimes")) { $installedRoot } else { $projectRoot }
if (-not $RuntimeRoot) {
    if ($env:LUBO_ASR_RUNTIMES) {
        $RuntimeRoot = $env:LUBO_ASR_RUNTIMES
    } else {
        $RuntimeRoot = Join-Path $appRoot "asr_runtimes"
    }
}
$RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null

$codeRevision = "4e7d9aaf4482a47cec1724807026b9b151926eb5"
$codeRepo = "https://github.com/FireRedTeam/FireRedASR2S.git"
$codeDir = Join-Path $RuntimeRoot "FireRedASR2S"
$modelDir = Join-Path $RuntimeRoot "fireredasr2-aed"
$venvDir = Join-Path $RuntimeRoot "firered-venv"
Write-Output "FireRed runtime root: $RuntimeRoot"

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) { throw "Git is required to install the pinned FireRed source checkout." }
$createdCodeCheckout = $false
if (-not (Test-Path -LiteralPath $codeDir)) {
    & $git.Source clone $codeRepo $codeDir
    if ($LASTEXITCODE -ne 0) { throw "Could not clone FireRedASR2S." }
    $createdCodeCheckout = $true
}
if (-not (Test-Path -LiteralPath (Join-Path $codeDir ".git"))) {
    throw "FireRed source directory exists but is not a Git checkout: $codeDir"
}
if ($createdCodeCheckout) {
    & $git.Source -C $codeDir checkout --detach $codeRevision
    if ($LASTEXITCODE -ne 0) { throw "Could not check out pinned FireRed revision $codeRevision." }
}
$actualRevision = (& $git.Source -C $codeDir rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $actualRevision -ne $codeRevision) {
    throw "FireRed code at $codeDir is $actualRevision; expected $codeRevision. Preserve it and choose another RuntimeRoot."
}
$dirty = & $git.Source -C $codeDir status --porcelain
if ($LASTEXITCODE -ne 0 -or $dirty) {
    throw "FireRed source checkout has local changes: $codeDir"
}

if (-not $PythonExe) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $pythonCommand = $launcher.Source
        $pythonArgs = @("-3.12")
    } else {
        $pythonCommand = (Get-Command python -ErrorAction Stop).Source
        $pythonArgs = @()
    }
} else {
    $pythonCommand = (Get-Command $PythonExe -ErrorAction Stop).Source
    $pythonArgs = @()
}
$pythonCheck = & $pythonCommand @pythonArgs -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
if ($LASTEXITCODE -ne 0) { throw "Unable to run the requested Python interpreter." }
$pythonVersion = [version]$pythonCheck.Trim()
if ($pythonVersion -lt [version]"3.11") {
    throw "FireRedASR2S requires Python 3.11 or newer; found $pythonVersion."
}

$venvPython = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $pythonCommand @pythonArgs -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw "Could not create the isolated FireRed environment." }
}
if (-not (Test-Path -LiteralPath $venvPython)) { throw "The FireRed environment was not created: $venvPython" }

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Could not update pip in the isolated FireRed environment." }
& $venvPython -m pip install -r (Join-Path $projectRoot "requirements-firered.txt")
if ($LASTEXITCODE -ne 0) { throw "Could not install the pinned FireRed CPU dependencies." }

if (-not $SkipModelDownload) {
    & $venvPython (Join-Path $PSScriptRoot "download_firered_model.py") `
        --model-dir $modelDir --code-dir $codeDir
    if ($LASTEXITCODE -ne 0) { throw "FireRed model download or verification failed." }
}

Write-Output "FireRed runtime Python: $venvPython"
Write-Output "FireRed source: $codeDir ($codeRevision)"
Write-Output "FireRed model: $modelDir"
Write-Output "To invoke: set LUBO_FIRERED_MODEL_DIR and LUBO_FIRERED_CODE_DIR as needed, then run scripts/firered_worker.py --job <job.json>."
