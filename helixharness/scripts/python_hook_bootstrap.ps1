# HelixHarness lifecycle hook bootstrap (Windows / PowerShell).
# Mirrors scripts/python_hook_bootstrap.sh: picks the first interpreter that
# actually runs Python, skipping Windows Store app-execution aliases.
param(
    [Parameter(Position = 0)]
    [string]$EventName = "",
    [Parameter(Position = 1)]
    [string]$Target = ""
)

# Hook hosts read stdout as UTF-8; PS5.1 defaults to the system codepage.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Get-PythonCommand {
    foreach ($candidate in @(
        @{ Exe = "python.exe";  Args = @() },
        @{ Exe = "python3.exe"; Args = @() },
        @{ Exe = "py.exe";      Args = @("-3") }
    )) {
        # Enumerate every PATH match: a Store alias may shadow a real
        # interpreter that appears later in PATH.
        foreach ($cmd in @(Get-Command $candidate.Exe -All -ErrorAction SilentlyContinue)) {
            $probeArgs = $candidate.Args + @("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)")
            & $cmd.Source @probeArgs 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                return @{ Exe = $cmd.Source; Args = $candidate.Args }
            }
        }
    }
    return $null
}

$python = Get-PythonCommand
if ($null -ne $python) {
    if (($Target -ne "") -and (Test-Path $Target)) {
        & $python.Exe @($python.Args + @($Target))
        exit $LASTEXITCODE
    }
    Write-Output '{"continue":true,"systemMessage":"HelixHarness Hook 未执行：插件内目标脚本不存在（runtime_target_missing）。"}'
    exit 0
}

if ($EventName -eq "SessionStart") {
    Write-Output '{"continue":true,"systemMessage":"HelixHarness Python preflight 未通过：需要 Python 3.10+ 且 python、python3 或 py 至少其一在 PATH 中可用。运行 HelixHarness 命令时会提供受控安装选项。"}'
}
# Lifecycle hooks must stay silent when Python is unavailable. The command
# preflight or SessionStart already emitted the actionable warning.
exit 0
