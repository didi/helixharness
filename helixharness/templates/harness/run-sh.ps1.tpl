# run-sh.ps1 — HelixHarness 项目钩子的 Windows 启动器
# 原生 Windows 上没有可用的 sh/head（bash 常指向 WSL 启动器），
# 本脚本定位 Git for Windows 自带的 bash.exe / sh.exe 来运行既有 linter，
# 并把 Git 的 usr/bin 加入 PATH（提供 head 等工具），输出截断为前 50 行。
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File docs/harness/linters/run-sh.ps1 <linter 脚本文件名>
# 始终 exit 0：linter 是 warning-only 提醒，不得阻断 PostToolUse。

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$ScriptName
)

[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

# 只接受文件名，防止路径逃逸。
$leaf = Split-Path -Leaf $ScriptName
if ($leaf -ne $ScriptName) { exit 0 }

# 定位 Git for Windows：优先 PATH 里的 git，退回常见安装目录。
$usrBin = $null
$gitCmd = Get-Command git.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $gitCmd) { $gitCmd = Get-Command git -ErrorAction SilentlyContinue | Select-Object -First 1 }
if ($null -ne $gitCmd) {
    # git.exe 可能位于 Git\cmd、Git\bin 或 Git\mingw64\bin，逐级向上找 usr\bin。
    $dir = Split-Path $gitCmd.Source
    for ($i = 0; $i -lt 4 -and $dir -and $null -eq $usrBin; $i++) {
        $candidate = Join-Path $dir "usr\bin"
        if (Test-Path (Join-Path $candidate "sh.exe")) { $usrBin = $candidate }
        $dir = Split-Path $dir
    }
}
if ($null -eq $usrBin) {
    foreach ($root in @("$env:ProgramFiles\Git", "${env:ProgramFiles(x86)}\Git", "$env:LOCALAPPDATA\Programs\Git")) {
        if ($root -and (Test-Path (Join-Path $root "usr\bin\sh.exe"))) {
            $usrBin = Join-Path $root "usr\bin"
            break
        }
    }
}
if ($null -eq $usrBin) {
    Write-Output "HelixHarness linter skipped: 未找到 Git for Windows 的 sh.exe。"
    exit 0
}

$shell = Join-Path $usrBin "bash.exe"
if (-not (Test-Path $shell)) { $shell = Join-Path $usrBin "sh.exe" }
$env:PATH = "$usrBin;$env:PATH"

# 本文件位于 docs/harness/linters/，linter 与它在同一目录。
$target = Join-Path (Split-Path -Parent $PSCommandPath) $leaf
if (-not (Test-Path $target)) { exit 0 }

& $shell ($target -replace '\\', '/') 2>&1 | Select-Object -First 50
exit 0
