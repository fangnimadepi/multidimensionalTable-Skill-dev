param([Parameter(ValueFromRemainingArguments=$true)][string[]]$TaskArguments)
$ErrorActionPreference = 'Stop'
$ScriptRoot = $PSScriptRoot
$DataRoot = if ($env:JINGYAN_HOME) { $env:JINGYAN_HOME } else { Join-Path $env:USERPROFILE '.jingyan' }
New-Item -ItemType Directory -Force $DataRoot | Out-Null
$Python = Join-Path $DataRoot 'venv\Scripts\python.exe'
$RuntimeReady = $false
if (Test-Path $Python) {
    try {
        & $Python -c 'import sys; sys.exit(not ((3,11) <= sys.version_info[:2] < (3,14)))' 2>$null
        $RuntimeReady = $LASTEXITCODE -eq 0
    } catch { $RuntimeReady = $false }
}
if (-not $RuntimeReady) {
    $Candidate = $env:JINGYAN_PYTHON
    if (-not $Candidate) {
        foreach ($Name in @('python', 'python3')) {
            $Found = Get-Command $Name -ErrorAction SilentlyContinue
            if ($Found) {
                & $Found.Source -c 'import sys; sys.exit(not ((3,11) <= sys.version_info[:2] < (3,14)))' 2>$null
                if ($LASTEXITCODE -eq 0) { $Candidate = $Found.Source; break }
            }
        }
    }
    if (-not $Candidate) {
        $Versions = Join-Path $env:USERPROFILE '.workbuddy\binaries\python\versions'
        foreach ($File in @(Get-ChildItem $Versions -Filter python.exe -Recurse -ErrorAction SilentlyContinue)) {
            & $File.FullName -c 'import sys; sys.exit(not ((3,11) <= sys.version_info[:2] < (3,14)))'
            if ($LASTEXITCODE -eq 0) { $Candidate = $File.FullName; break }
        }
    }
    if (-not $Candidate) {
        $Uv = if ($env:JINGYAN_UV) { $env:JINGYAN_UV } else { Join-Path $DataRoot 'tools\uv.exe' }
        if (-not (Test-Path $Uv)) {
            $Found = Get-Command uv -ErrorAction SilentlyContinue
            if ($Found) { $Uv = $Found.Source } else {
                $Installer = Join-Path $DataRoot 'uv-install.ps1'
                Invoke-WebRequest 'https://astral.sh/uv/install.ps1' -OutFile $Installer -UseBasicParsing
                $env:UV_INSTALL_DIR = Join-Path $DataRoot 'tools'
                $env:UV_NO_MODIFY_PATH = '1'
                & $Installer
                if (-not (Test-Path $Uv)) { throw 'uv 初始化失败，请检查网络和运行权限。' }
            }
        }
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $DataRoot 'python'
        $env:UV_CACHE_DIR = Join-Path $DataRoot 'cache'
        $EnvironmentPath = Join-Path $DataRoot 'venv'
        if (Test-Path $EnvironmentPath) {
            if ((Get-Item $EnvironmentPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'venv 路径为链接，请检查个人配置。'
            }
            Move-Item $EnvironmentPath (Join-Path $DataRoot ('venv-incomplete-' + [guid]::NewGuid().ToString('N').Substring(0,10)))
        }
        & $Uv venv --python 3.12 (Join-Path $DataRoot 'venv')
        if ($LASTEXITCODE -ne 0) { throw 'Python 初始化失败，请检查网络和运行权限。' }
        $env:JINGYAN_UV = $Uv
        $Candidate = $Python
    }
    & $Candidate (Join-Path $ScriptRoot 'bootstrap.py') --data-dir $DataRoot
} else {
    & $Python (Join-Path $ScriptRoot 'bootstrap.py') --data-dir $DataRoot
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$Config = if ($env:JINGYAN_CONFIG) { $env:JINGYAN_CONFIG } else { Join-Path $DataRoot '鲸研配置.json' }
if (-not $TaskArguments -or $TaskArguments[0] -eq 'init') { exit 0 }
$Action = $TaskArguments[0]
$Remaining = @($TaskArguments | Select-Object -Skip 1)
switch ($Action) {
    'configure' { & $Python (Join-Path $ScriptRoot 'model_setup.py') --config $Config @Remaining }
    'reexport' { & $Python (Join-Path $ScriptRoot 'reexport_checked.py') --config $Config @Remaining }
    'plan-batch' { & $Python (Join-Path $ScriptRoot 'plan_batch.py') @Remaining }
    'feishu' { & $Python (Join-Path $ScriptRoot 'feishu_batch.py') --config $Config @Remaining }
    default { & $Python (Join-Path $ScriptRoot 'local_reports.py') --config $Config @TaskArguments }
}
exit $LASTEXITCODE
