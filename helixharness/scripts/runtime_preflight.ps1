# Native Windows entrypoint; works in Windows PowerShell 5.1 and PowerShell 7.
param(
    [ValidateSet('check', 'exec', 'hook', 'install-python')]
    [string]$Action = 'check',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RuntimeArguments
)
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
# One exhausted-budget guard across every discovery source, so several broken
# candidates cannot add up past the host's hook deadline.
$ProbeBudgetMilliseconds = 20000

function Get-ProbeMilliseconds([int]$Cap = 10000) {
    $remaining = $script:ProbeBudgetMilliseconds - [int]$script:ProbeClock.ElapsedMilliseconds
    if ($remaining -le 0) { return 0 }
    return [int][Math]::Min($Cap, $remaining)
}

function Join-NativeArguments([string[]]$Values) {
    # Windows CRT quoting, including embedded quotes and trailing backslashes.
    # Calling a native executable with & loses embedded quotes in PowerShell 5.1.
    return (($Values | ForEach-Object {
        '"' + (($_ -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
    }) -join ' ')
}

function Invoke-CapturedProcess([string]$Executable, [string[]]$Values, [int]$TimeoutMilliseconds = 10000) {
    # Interpreter probes must not be able to stall a hook. A broken alias or a
    # shim waiting on input would otherwise outlive the host's hook deadline.
    if ($TimeoutMilliseconds -le 0) { return $null }
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $extension = [IO.Path]::GetExtension($Executable)
    if ($extension -in @('.cmd', '.bat')) {
        $info.FileName = $env:ComSpec
        if (-not $info.FileName) { $info.FileName = 'cmd.exe' }
        $info.Arguments = '/d /s /c "' + (Join-NativeArguments (@($Executable) + $Values)) + '"'
    } else {
        $info.FileName = $Executable
        $info.Arguments = Join-NativeArguments $Values
    }
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $info.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
    $process = [System.Diagnostics.Process]::Start($info)
    try {
        try { $process.StandardInput.Close() } catch {}
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            try { $process.Kill() } catch {}
            try { $process.WaitForExit(2000) | Out-Null } catch {}
            return $null
        }
        # A probe can exit while a grandchild keeps the pipe open. Bound the
        # drain so a leaked handle cannot turn into an unbounded hook hang.
        if (-not $stdout.Wait(2000) -or -not $stderr.Wait(2000)) { return $null }
        return [pscustomobject]@{
            exit_code = $process.ExitCode
            output = $stdout.Result
            error = $stderr.Result
        }
    } finally { $process.Dispose() }
}

function Invoke-HelixPython($Python, [string[]]$Values, [bool]$RedirectInput) {
    $inputBytes = $null
    if ($RedirectInput) {
        # Buffer the finite hook payload before starting Python. This avoids a
        # Windows pipe-lifetime race where the child can keep PowerShell's
        # redirected console input open while CopyTo waits for EOF.
        $inputStream = [Console]::OpenStandardInput()
        $buffer = New-Object System.IO.MemoryStream
        try {
            $inputStream.CopyTo($buffer)
            $inputBytes = $buffer.ToArray()
        } finally { $buffer.Dispose() }
    }
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Python.executable
    $pythonValues = $Values
    if ($RedirectInput) {
        # Windows PowerShell 5.1 exposes no ProcessStartInfo.StandardInputEncoding,
        # and its default writer can add a UTF-8 BOM. Decode only hook stdin as
        # utf-8-sig so both Windows PowerShell and PowerShell 7 transports work;
        # leave stdout/stderr on the runtime's ordinary UTF-8 configuration.
        $pythonValues = @(
            '-c',
            "import runpy,sys; sys.stdin.reconfigure(encoding='utf-8-sig'); runpy.run_path(sys.argv[1], run_name='__main__')"
        ) + $Values
    }
    $info.Arguments = Join-NativeArguments (@($Python.arguments) + $pythonValues)
    $info.UseShellExecute = $false
    $info.RedirectStandardInput = $RedirectInput
    $process = [System.Diagnostics.Process]::Start($info)
    try {
        if ($RedirectInput) {
            # Keep hook payloads byte-for-byte intact. Windows PowerShell 5.1
            # decodes redirected Console.In with its legacy console code page,
            # so ReadToEnd followed by UTF-8 encoding corrupts non-ASCII JSON.
            # Some Windows hosts also prepend a UTF-8 BOM to redirected text;
            # JSON hooks require that transport marker to be removed.
            $offset = 0
            if ($inputBytes.Length -ge 3 -and $inputBytes[0] -eq 0xEF -and
                    $inputBytes[1] -eq 0xBB -and $inputBytes[2] -eq 0xBF) {
                $offset = 3
            }
            if ($inputBytes.Length -gt $offset) {
                $process.StandardInput.BaseStream.Write(
                    $inputBytes, $offset, $inputBytes.Length - $offset
                )
            }
            $process.StandardInput.Close()
        }
        $process.WaitForExit()
        return $process.ExitCode
    } finally { $process.Dispose() }
}

function Find-HelixPython([string]$Explicit) {
    $status = 'missing'
    $version = $null
    # install-python runs this again after winget, so each lookup gets its own budget.
    $script:ProbeClock = [System.Diagnostics.Stopwatch]::StartNew()
    $probe = 'import sys; print(str(sys.version_info.major)+chr(46)+str(sys.version_info.minor)+chr(46)+str(sys.version_info.micro))'
    $explicitRequested = $PSBoundParameters.ContainsKey('Explicit')
    if ($explicitRequested -and ([string]::IsNullOrWhiteSpace($Explicit) -or $Explicit -match '[\x00-\x1F\x7F]')) {
        return @{ status = 'unusable'; version = $null; executable = $null; arguments = @() }
    }
    $names = @('python.exe', 'python3.exe', 'py.exe')
    if ($explicitRequested) { $names = @($Explicit) }
    foreach ($name in $names) {
        $candidates = @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)
        if ($explicitRequested -and [IO.Path]::IsPathRooted($name) -and (Test-Path -LiteralPath $name -PathType Leaf)) {
            $candidates = @([pscustomobject]@{ Source = $name })
        }
        if ($explicitRequested) { $candidates = @($candidates | Select-Object -First 1) }
        foreach ($candidate in $candidates) {
            # Do not launch the Microsoft Store's placeholder executable.
            if ($candidate.Source -match '[\\/]Microsoft[\\/]WindowsApps[\\/]') { continue }
            $prefix = @()
            if ([IO.Path]::GetFileName($name) -eq 'py.exe') { $prefix = @('-3') }
            try {
                $probed = Invoke-CapturedProcess $candidate.Source (@($prefix) + @('-c', $probe)) (Get-ProbeMilliseconds)
                if ($null -eq $probed -or $probed.exit_code -ne 0) {
                    $status = 'unusable'
                    continue
                }
                $version = "$($probed.output)".Trim()
                if ($version -notmatch '^\d+\.\d+\.\d+$') {
                    $status = 'unusable'
                    continue
                }
                if ([version]$version -ge [version]'3.10') {
                    return @{ status = 'ready'; version = "$version"; executable = $candidate.Source; arguments = @($prefix) + @('-X', 'utf8') }
                }
                $status = 'unsupported_version'
            } catch { $status = 'unusable' }
        }
    }
    if ($explicitRequested) {
        if ($status -eq 'missing') { $status = 'unusable' }
        return @{ status = $status; version = $null; executable = $null; arguments = @() }
    }
    # uv-managed interpreters are installed independently from uv's cache.
    # Read those directories directly so Codex's workspace sandbox does not
    # need write access to %LOCALAPPDATA%\uv\cache just to locate Python.
    $managedRoots = @(
        $env:UV_PYTHON_INSTALL_DIR,
        $(if ($env:APPDATA) { Join-Path $env:APPDATA 'uv\python' }),
        $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'uv\python' }),
        $(if ($env:USERPROFILE) { Join-Path $env:USERPROFILE '.local\share\uv\python' })
    ) | Where-Object { $_ } | Select-Object -Unique
    foreach ($root in $managedRoots) {
        try {
            $directories = @($root) + @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending | Select-Object -ExpandProperty FullName)
            foreach ($directory in $directories) {
                foreach ($managed in @((Join-Path $directory 'python.exe'), (Join-Path $directory 'Scripts\python.exe'))) {
                    if (-not (Test-Path -LiteralPath $managed -PathType Leaf)) { continue }
                    $probed = Invoke-CapturedProcess $managed @('-c', $probe) (Get-ProbeMilliseconds)
                    if ($null -eq $probed -or $probed.exit_code -ne 0) { continue }
                    $version = "$($probed.output)".Trim()
                    if ($version -match '^\d+\.\d+\.\d+$' -and
                            [version]$version -ge [version]'3.10') {
                        return @{ status = 'ready'; version = "$version"; executable = $managed; arguments = @('-X', 'utf8') }
                    }
                }
            }
        } catch { $status = 'unusable' }
    }
    # Codex Desktop can expose uv while omitting its managed Python directory
    # from the hook PATH. Reuse an already-installed interpreter, but never let
    # preflight download one implicitly.
    foreach ($uv in @(Get-Command 'uv.exe' -CommandType Application -All -ErrorAction SilentlyContinue)) {
        try {
            $found = Invoke-CapturedProcess $uv.Source @('--no-python-downloads', 'python', 'find', '>=3.10') (Get-ProbeMilliseconds 10000)
            if ($null -eq $found -or $found.exit_code -ne 0) { continue }
            $managed = @("$($found.output)" -split '\r?\n' | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -Last 1)
            if ($managed.Count -eq 0) { continue }
            $managed = $managed[0]
            if (-not (Test-Path -LiteralPath $managed -PathType Leaf)) { continue }
            $probed = Invoke-CapturedProcess $managed @('-c', $probe) (Get-ProbeMilliseconds)
            if ($null -eq $probed -or $probed.exit_code -ne 0) { $status = 'unusable'; continue }
            $version = "$($probed.output)".Trim()
            if ($version -match '^\d+\.\d+\.\d+$' -and
                    [version]$version -ge [version]'3.10') {
                return @{ status = 'ready'; version = "$version"; executable = $managed; arguments = @('-X', 'utf8') }
            }
            $status = 'unusable'
        } catch { $status = 'unusable' }
    }
    return @{ status = $status; version = $null; executable = $null; arguments = @() }
}

function Write-Check($python) {
    $gitStatus = 'missing'
    if (Get-Command git.exe -CommandType Application -ErrorAction SilentlyContinue) { $gitStatus = 'ready' }
    $next = 'continue'
    if ($gitStatus -ne 'ready') { $next = 'install_git' }
    elseif ($python.status -ne 'ready') { $next = 'install_python_or_disable_telemetry' }
    @{
        schema_version = '1.0'; ok = ($next -eq 'continue')
        python = @{ status = $python.status; minimum_version = '3.10'; version = $python.version
            executable = $python.executable; arguments = @($python.arguments) }
        git = @{ status = $gitStatus }; next_action = $next
    } | ConvertTo-Json -Depth 5 -Compress
}

if ($Action -eq 'exec' -and $RuntimeArguments -and $RuntimeArguments[0] -eq '--python') {
    $explicit = ''
    if ($RuntimeArguments.Count -gt 1) { $explicit = $RuntimeArguments[1] }
    $RuntimeArguments = @($RuntimeArguments | Select-Object -Skip 2)
    $python = Find-HelixPython -Explicit $explicit
} else {
    $python = Find-HelixPython
}
switch ($Action) {
    'check' { Write-Check $python; exit 0 }
    'install-python' {
        if ($RuntimeArguments -notcontains '--user-confirmed') {
            '{"ok":false,"installed":false,"error_code":"python_install_confirmation_required"}'
            exit 2
        }
        if ($python.status -eq 'ready') { Write-Check $python; exit 0 }
        if (-not (Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue)) {
            '{"ok":false,"installed":false,"error_code":"python_package_manager_unavailable"}'
            exit 1
        }
        & winget.exe install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements | Out-Host
        if ($LASTEXITCODE -ne 0) {
            '{"ok":false,"installed":false,"error_code":"python_install_failed"}'
            exit 1
        }
        # Preserve the process PATH while picking up the installer's persisted additions.
        $env:PATH = $env:PATH + ';' + [Environment]::GetEnvironmentVariable('PATH', 'User') + ';' + [Environment]::GetEnvironmentVariable('PATH', 'Machine')
        $python = Find-HelixPython
        Write-Check $python
        if ($python.status -ne 'ready') { exit 1 }
        exit 0
    }
    'hook' {
        if ($python.status -ne 'ready') {
            if ($RuntimeArguments -and $RuntimeArguments[0] -eq 'SessionStart') {
                '{"continue":true,"systemMessage":"HelixHarness requires Python 3.10+ (python.exe, python3.exe or py -3)."}'
            }
            exit 0
        }
        if ($RuntimeArguments.Count -ne 2 -or -not (Test-Path -LiteralPath $RuntimeArguments[1] -PathType Leaf)) {
            '{"continue":true,"systemMessage":"HelixHarness runtime_target_missing"}'
            exit 0
        }
        $code = Invoke-HelixPython $python @($RuntimeArguments[1]) $true
        exit $code
    }
    'exec' {
        if ($python.status -ne 'ready') { Write-Check $python; exit 2 }
        if (-not $RuntimeArguments) { exit 2 }
        $code = Invoke-HelixPython $python $RuntimeArguments $false
        exit $code
    }
}
