# HelixHarness runtime preflight (Windows / PowerShell).
# Mirrors scripts/runtime_preflight.sh: same JSON contract for `check`.
# Probes python.exe, python3.exe and `py -3`; Windows Store app-execution
# aliases (WindowsApps stubs) fail the version probe and are skipped.
param(
    [Parameter(Position = 0)]
    [string]$Action = "check"
)

$MinimumPython = "3.10"

# Callers read stdout as UTF-8; PS5.1 defaults to the system codepage.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Get-PythonProbe {
    # Returns @{ Version = "x.y.z"; Command = "python" } for the first
    # candidate that runs a real Python, plus the best unsupported probe.
    $candidates = @(
        @{ Exe = "python.exe";  Args = @();      Command = "python" },
        @{ Exe = "python3.exe"; Args = @();      Command = "python3" },
        @{ Exe = "py.exe";      Args = @("-3");  Command = "py -3" }
    )
    $foundAny = $false
    $unsupported = $null
    foreach ($candidate in $candidates) {
        # Enumerate every PATH match: a Store alias may shadow a real
        # interpreter that appears later in PATH.
        $cmds = @(Get-Command $candidate.Exe -All -ErrorAction SilentlyContinue)
        if ($cmds.Count -eq 0) { continue }
        $foundAny = $true
        foreach ($cmd in $cmds) {
            # Single quotes only: PS5.1 misquotes embedded double quotes
            # when passing arguments to native commands.
            $probeArgs = $candidate.Args + @("-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])")
            $output = & $cmd.Source @probeArgs 2>$null
            if ($LASTEXITCODE -ne 0) { continue }
            $version = (($output | Select-Object -First 1) + "").Trim()
            if ($version -notmatch '^\d+\.\d+\.\d+$') { continue }
            if ($null -eq $unsupported) {
                $unsupported = @{ Version = $version; Command = $candidate.Command }
            }
            $parts = $version.Split(".")
            if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10)) {
                return @{ Status = "ready"; Version = $version; Command = $candidate.Command }
            }
            break
        }
    }
    if ($null -ne $unsupported) {
        return @{ Status = "unsupported_version"; Version = $unsupported.Version; Command = $unsupported.Command }
    }
    if ($foundAny) {
        return @{ Status = "unusable"; Version = $null; Command = $null }
    }
    return @{ Status = "missing"; Version = $null; Command = $null }
}

function Get-GitStatus {
    if (Get-Command git.exe -ErrorAction SilentlyContinue) { return "ready" }
    if (Get-Command git -ErrorAction SilentlyContinue) { return "ready" }
    return "missing"
}

function ConvertTo-JsonNullable {
    param([string]$Value)
    if ($null -eq $Value -or $Value -eq "") { return "null" }
    return '"' + ($Value -replace '\\', '\\' -replace '"', '\"') + '"'
}

if ($Action -eq "check") {
    $python = Get-PythonProbe
    $git = Get-GitStatus
    if ($git -ne "ready") {
        $ok = "false"; $nextAction = "install_git"
    } elseif ($python.Status -ne "ready") {
        $ok = "false"; $nextAction = "install_python_or_disable_telemetry"
    } else {
        $ok = "true"; $nextAction = "continue"
    }
    $versionJson = ConvertTo-JsonNullable $python.Version
    $commandJson = ConvertTo-JsonNullable $python.Command
    $json = '{"schema_version":"1.0","ok":' + $ok + ',"python":{"status":"' + $python.Status + '","minimum_version":"' + $MinimumPython + '","version":' + $versionJson + ',"command":' + $commandJson + '},"git":{"status":"' + $git + '"},"next_action":"' + $nextAction + '"}'
    Write-Output $json
    exit 0
}

Write-Error "usage: runtime_preflight.ps1 check"
exit 2
