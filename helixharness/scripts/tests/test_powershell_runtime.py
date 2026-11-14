"""Execute PowerShell adapters; fake only OS discovery and synthetic interpreters.

Windows runs both Windows PowerShell and pwsh when installed. Other systems can
set HELIXHARNESS_TEST_PWSH to run the same fixtures under PowerShell 7.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SHELLS = list(dict.fromkeys(p for p in (
    os.environ.get('HELIXHARNESS_TEST_PWSH'), shutil.which('pwsh'), shutil.which('powershell.exe'),
) if p))


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


HARNESS = r'''
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONUTF8 = '1'
$global:HelixTestPlan = ConvertFrom-Json -InputObject __PLAN__
$global:HelixTestPython = __PYTHON__
$global:HelixTestLog = __LOG__
$i = 0
foreach ($entry in @($global:HelixTestPlan.Candidates)) {
    $source = "HelixTestPython$i"
    $implementation = {
        # Native commands flatten array-valued arguments. Our discovery mock
        # is a PowerShell function, so reproduce that native binding here.
        $arguments = @($args | ForEach-Object { $_ })
        $record = @{name=$entry.Name; arguments=@($arguments)} | ConvertTo-Json -Compress -Depth 5
        Add-Content -LiteralPath $global:HelixTestLog -Value $record -Encoding UTF8
        if ($entry.Name -eq 'py.exe') {
            if ($arguments[0] -ne '-3') { $global:LASTEXITCODE=91; return }
            $arguments = @($arguments | Select-Object -Skip 1)
        }
        if ($entry.Mode -eq 'broken') { $global:LASTEXITCODE=1; return }
        if ($entry.Mode -eq 'malformed') { 'not a version'; $global:LASTEXITCODE=0; return }
        if ($arguments[0] -eq '-c') {
            $version = ($entry.Version -split '\.') -join ','
            $code = "import sys; sys.version_info=($version); exec(sys.argv[1])"
            & $global:HelixTestPython -c $code $arguments[1]
            return
        }
        & $global:HelixTestPython @arguments
    }.GetNewClosure()
    Set-Item -Path "Function:global:$source" -Value $implementation
    $entry | Add-Member -NotePropertyName Source -NotePropertyValue $source
    $i++
}
function global:Get-Command {
    param([string]$Name, [switch]$All, $ErrorAction, $CommandType)
    if ($Name -in @('git', 'git.exe')) {
        if ($global:HelixTestPlan.Git) { [pscustomobject]@{Source='synthetic-git.exe'} }
        return
    }
    foreach ($candidate in @($global:HelixTestPlan.Candidates)) {
        if ($candidate.Name -eq $Name) { [pscustomobject]@{Source=$candidate.Source} }
    }
}
__BEFORE__
& __TARGET__ __ARGUMENTS__
exit $LASTEXITCODE
'''


class PowerShellRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        names = {Path(shell).stem.lower() for shell in SHELLS}
        if os.name == 'nt' and os.environ.get('HELIXHARNESS_REQUIRE_POWERSHELL_TESTS') == '1' and not {'pwsh', 'powershell'} <= names:
            raise RuntimeError('Windows CI must exercise both powershell.exe and pwsh')
        if not SHELLS:
            if os.name == 'nt':
                raise RuntimeError('Windows runtime tests require PowerShell')
            raise unittest.SkipTest('PowerShell unavailable; set HELIXHARNESS_TEST_PWSH to enable')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='helix PS 测试 ')
        self.addCleanup(self.temp.cleanup)
        # PowerShell expands Windows 8.3 aliases (RUNNER~1) in script paths.
        self.folder = Path(self.temp.name).resolve()
        self.log = self.folder/'calls.jsonl'
        self.target = self.folder/'target with spaces.py'
        self.target.write_text('import json, sys\nprint(json.dumps({"stdin":sys.stdin.read(), "argv":sys.argv[1:]}, ensure_ascii=False))\nsys.exit(7)\n', encoding='utf-8')

    def invoke(self, shell, script, arguments=(), candidates=(), git=True, before='', stdin=''):
        self.log.unlink(missing_ok=True)
        plan = {'Candidates': list(candidates), 'Git': git}
        harness = HARNESS.replace('__PLAN__', quote(json.dumps(plan))).replace('__PYTHON__', quote(sys.executable))
        harness = harness.replace('__LOG__', quote(self.log)).replace('__BEFORE__', before)
        harness = harness.replace('__TARGET__', quote(script)).replace('__ARGUMENTS__', ' '.join(quote(a) for a in arguments))
        path = self.folder/'harness.ps1'
        path.write_text(harness, encoding='utf-8-sig')
        return subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(path)],
                              input=stdin, capture_output=True, text=True, encoding='utf-8', timeout=30)

    def candidate(self, name='python.exe', version='3.10.0', mode='ready'):
        return {'Name': name, 'Version': version, 'Mode': mode}

    def check(self, shell, **kwargs):
        result = self.invoke(shell, ROOT/'scripts/runtime_preflight.ps1', ['check'], **kwargs)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual('', result.stderr)
        return json.loads(result.stdout.lstrip('\ufeff'))

    def test_preflight_enforces_minimum_and_reports_valid_interpreter(self):
        for shell in SHELLS:
            for version, status in [('3.9.99', 'unsupported_version'), ('3.10.0', 'ready'), ('4.0.0', 'ready')]:
                with self.subTest(shell=shell, version=version):
                    payload = self.check(shell, candidates=[self.candidate(version=version)])
                    self.assertEqual(status, payload['python']['status'])
                    self.assertEqual(status == 'ready', payload['ok'])
                    self.assertEqual(version, payload['python']['version'])
                    self.assertEqual('3.10', payload['python']['minimum_version'])

    def test_probe_skips_broken_alias_and_old_shadow_to_find_a_usable_match(self):
        for shell in SHELLS:
            for mode in ('broken', 'malformed', 'ready'):
                with self.subTest(shell=shell, mode=mode):
                    payload = self.check(shell, candidates=[self.candidate(version='3.9.0', mode=mode), self.candidate(version='3.12.1')])
                    self.assertTrue(payload['ok'])
                    self.assertEqual('3.12.1', payload['python']['version'])

    def test_launcher_prefix_and_git_failure_precedence(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                payload = self.check(shell, candidates=[self.candidate(mode='broken'), self.candidate('py.exe')])
                self.assertEqual('py -3', payload['python']['command'])
                calls = [json.loads(line.lstrip('\ufeff')) for line in self.log.read_text(encoding='utf-8-sig').splitlines()]
                self.assertEqual('-3', calls[-1]['arguments'][0])
                payload = self.check(shell, candidates=[], git=False)
                self.assertEqual('missing', payload['python']['status'])
                self.assertEqual('install_git', payload['next_action'])
                payload = self.check(shell, candidates=[self.candidate(mode='broken')])
                self.assertEqual('unusable', payload['python']['status'])

    def test_windowsapps_alias_is_not_executed(self):
        alias = self.folder/'Microsoft/WindowsApps/python.ps1'
        alias.parent.mkdir(parents=True)
        sentinel = self.folder/'alias-executed'
        alias.write_text('Set-Content -LiteralPath '+quote(sentinel)+" -Value bad\n'3.12.0'\n$global:LASTEXITCODE=0\n", encoding='utf-8-sig')
        for shell in SHELLS:
            with self.subTest(shell=shell):
                before = "$global:HelixTestPlan.Candidates[0].Source = " + quote(alias)
                result = self.invoke(shell, ROOT/'scripts/runtime_preflight.ps1', ['check'], [self.candidate(), self.candidate('python3.exe')], before=before)
                self.assertFalse(sentinel.exists(), result.stdout + result.stderr)
                self.assertEqual('python3', json.loads(result.stdout.lstrip('\ufeff'))['python']['command'])

    def test_hook_rejects_python39_warns_on_start_and_stays_silent_on_end(self):
        for shell in SHELLS:
            for event in ('SessionStart', 'SessionEnd'):
                with self.subTest(shell=shell, event=event):
                    result = self.invoke(shell, ROOT/'scripts/python_hook_bootstrap.ps1', [event, self.target], [self.candidate(version='3.9.9')])
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    if event == 'SessionStart':
                        self.assertIn('3.10+', json.loads(result.stdout.lstrip('\ufeff'))['systemMessage'])
                    else:
                        self.assertEqual('', result.stdout)

    def test_hook_preserves_unicode_stdin_and_target_exit_code_for_launcher(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.invoke(shell, ROOT/'scripts/python_hook_bootstrap.ps1', ['SessionStart', self.target],
                                     [self.candidate(mode='broken'), self.candidate('py.exe')], stdin='测试 input with spaces')
                self.assertEqual(7, result.returncode, result.stdout + result.stderr)
                self.assertEqual({'stdin': '测试 input with spaces', 'argv': []}, json.loads(result.stdout.lstrip('\ufeff')))

    def test_hook_missing_target_is_a_controlled_nonblocking_message(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.invoke(shell, ROOT/'scripts/python_hook_bootstrap.ps1', ['SessionStart', self.folder/'missing.py'], [self.candidate()])
                self.assertEqual(0, result.returncode)
                self.assertIn('runtime_target_missing', json.loads(result.stdout.lstrip('\ufeff'))['systemMessage'])

    def test_unknown_preflight_action_fails_without_installing(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.invoke(shell, ROOT/'scripts/runtime_preflight.ps1', ['install-python'])
                self.assertEqual(2, result.returncode)
                self.assertIn('usage:', result.stderr)
                self.assertFalse(self.log.exists())

    def linter(self):
        launcher = self.folder/'run-sh.ps1'
        launcher.write_bytes((ROOT/'templates/harness/run-sh.ps1.tpl').read_bytes())
        git = self.folder/'Git/cmd/git.exe'
        git.parent.mkdir(parents=True, exist_ok=True)
        git.touch()
        native = self.folder/'Git/usr/bin/sh.exe'
        native.parent.mkdir(parents=True, exist_ok=True)
        native.touch()
        before = r'''
function global:Get-Command {
    param($Name, [switch]$All, $ErrorAction)
    if ($Name -in @('git.exe','git')) { [pscustomobject]@{Source=__GIT__} }
}
function global:Join-Path {
    param([string]$Path, [string]$ChildPath)
    if ($ChildPath -eq 'bash.exe') { return 'HelixTestShell' }
    Microsoft.PowerShell.Management\Join-Path -Path $Path -ChildPath $ChildPath
}
function global:Test-Path {
    param([string]$Path)
    if ($Path -eq 'HelixTestShell') { return $true }
    Microsoft.PowerShell.Management\Test-Path -Path $Path
}
function global:HelixTestShell {
    param([string]$Target)
    @{target=$Target; path=$env:PATH} | ConvertTo-Json -Compress | Set-Content -LiteralPath $global:HelixTestLog -Encoding UTF8
    for ($line=0; $line -lt 80; $line++) { Write-Output "line$line" }
    $global:LASTEXITCODE=7
}
'''.replace('__GIT__', quote(git))
        return launcher, before

    def test_linter_locates_git_shell_limits_output_and_remains_warning_only(self):
        launcher, before = self.linter()
        target = self.folder/'检查 linter.sh'
        target.write_text('# synthetic linter\n', encoding='utf-8')
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.invoke(shell, launcher, [target.name], before=before)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual(['line'+str(i) for i in range(50)], result.stdout.lstrip('\ufeff').splitlines())
                record = json.loads(self.log.read_text(encoding='utf-8-sig'))
                self.assertEqual(target.as_posix(), record['target'])
                self.assertTrue(record['path'].startswith(str(self.folder/'Git/usr/bin')))

    def test_linter_rejects_path_escape_and_missing_targets_without_execution(self):
        launcher, before = self.linter()
        outside = self.folder.parent/'outside.sh'
        for shell in SHELLS:
            for name in ('../outside.sh', '..\\outside.sh', str(outside), 'missing.sh'):
                with self.subTest(shell=shell, name=name):
                    result = self.invoke(shell, launcher, [name], before=before)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertEqual('', result.stdout)
                    self.assertFalse(self.log.exists())

    def test_linter_missing_git_is_a_nonblocking_warning(self):
        launcher, _ = self.linter()
        before = '$env:ProgramFiles='+quote(self.folder/'absent')+'; ${env:ProgramFiles(x86)}='+quote(self.folder/'absent')+'; $env:LOCALAPPDATA='+quote(self.folder/'absent')
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.invoke(shell, launcher, ['missing.sh'], git=False, before=before)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn('linter skipped', result.stdout)
                self.assertFalse(self.log.exists())


if __name__ == '__main__':
    unittest.main()
