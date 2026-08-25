# Stage 3 (Windows): simulate the real end-user flow against the built binary.
#   build check -> configure -> run install.ps1 WITH service -> verify the
#   service-managed daemon is answering -> live usage test -> re-run install over
#   the running daemon (self-update regression) -> uninstall + verify.
# Each phase is recorded into Allure (parentSuite=OS, suite=phase). The job fails
# if install, usage, update, or uninstall fails.
#
# Env: ARCHIVE (path to the release zip), OSNAME (Windows), PLATFORM.
$ErrorActionPreference = 'Continue'

$AR = 'allure-results'
New-Item -ItemType Directory -Force -Path $AR | Out-Null

function Emit($name, $suite, $status) {
    python tests/smoke/scenario.py emit --alluredir $AR `
        --allure-os $env:OSNAME --allure-suite $suite --allure-name $name --status $status
}
function SteerholmUp {
    try { return ((Invoke-RestMethod -Uri 'http://127.0.0.1:4767/healthz' -TimeoutSec 1 -ErrorAction Stop).service -eq 'steerholm') }
    catch { return $false }
}

# ── Build: the freshly built binary runs ───────────────────────────
$tmp = Join-Path $env:TEMP 'holm-e2e'
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
Expand-Archive -Path $env:ARCHIVE -DestinationPath $tmp -Force
$binTmp = Join-Path $tmp 'holm.exe'
& $binTmp version *> $null
if ($LASTEXITCODE -eq 0) { Emit "built binary runs ($env:PLATFORM)" Build passed }
else { Emit "built binary runs ($env:PLATFORM)" Build failed }

# ── Configure before the daemon starts (the service reads this config) ──
$out = python tests/smoke/scenario.py configure --holm $binTmp
$token = ($out | Select-String -Pattern '^TOKEN=(.+)$').Matches.Groups[1].Value

$env:PYTHON_KEYRING_BACKEND = 'keyrings.alt.file.PlaintextKeyring'
# The logon-task-spawned daemon must use the SAME (file) keyring backend as the
# configure step, or it can't verify the token (401). The task inherits the
# User-scope environment, not this shell's, so set it there too. CI-headless
# only: real users share one in-session OS keyring across configure and daemon.
[Environment]::SetEnvironmentVariable('PYTHON_KEYRING_BACKEND', 'keyrings.alt.file.PlaintextKeyring', 'User')

# ── Install via the real script, WITH service (logon task) registration ──
$env:STEERHOLM_LOCAL_ARCHIVE = $env:ARCHIVE
powershell -ExecutionPolicy Bypass -File scripts/install.ps1

# ── Verify the daemon is answering. On a hosted (non-interactive) runner the
# Interactive logon task may not fire; if so, run the identical binary the task
# launches (holmd.exe) so usage is still tested against the real daemon. ──
$up = $false
for ($i = 0; $i -lt 20; $i++) { if (SteerholmUp) { $up = $true; break }; Start-Sleep -Seconds 1 }
$triggerFired = $up
if (-not $up) {
    Write-Host "logon task did not fire on this non-interactive runner; starting holmd.exe directly (same daemon)"
    $daemon = Join-Path $env:LOCALAPPDATA 'steerholm\bin\holmd.exe'
    if (Test-Path $daemon) { Start-Process $daemon }
    for ($i = 0; $i -lt 20; $i++) { if (SteerholmUp) { $up = $true; break }; Start-Sleep -Seconds 1 }
}
if ($up) { Emit "service-managed daemon up ($env:PLATFORM)" Install passed }
else { Emit "service-managed daemon up ($env:PLATFORM)" Install failed }
if ($up -and -not $triggerFired) {
    Write-Host "NOTE: usage validated against the real daemon binary; the logon trigger itself is not exercisable on a non-interactive hosted runner."
}

# ── Usage: live test against the running daemon ─────────────────────
$usageOk = $false
if ($up) {
    python tests/smoke/scenario.py check --url http://127.0.0.1:4767/mcp --token $token `
        --alluredir $AR --allure-os $env:OSNAME --allure-suite Usage --allure-name "live usage ($env:PLATFORM)"
    $usageOk = ($LASTEXITCODE -eq 0)
} else {
    Emit "live usage skipped: daemon not up ($env:PLATFORM)" Usage failed
}

# ── Update: re-run install.ps1 over the RUNNING daemon. Regression guard for
#    the self-update path — a running holm.exe/holmd.exe can't be overwritten,
#    so install.ps1 must rename-aside + restart, and the daemon must come back. ──
$updateOk = $false
if ($up) {
    $env:STEERHOLM_LOCAL_ARCHIVE = $env:ARCHIVE
    powershell -ExecutionPolicy Bypass -File scripts/install.ps1
    $installRc = $LASTEXITCODE
    # daemon back up? (mirror the install-phase fallback for non-interactive runners)
    $reup = $false
    for ($i = 0; $i -lt 20; $i++) { if (SteerholmUp) { $reup = $true; break }; Start-Sleep -Seconds 1 }
    if (-not $reup) {
        $daemon = Join-Path $env:LOCALAPPDATA 'steerholm\bin\holmd.exe'
        if (Test-Path $daemon) { Start-Process $daemon }
        for ($i = 0; $i -lt 20; $i++) { if (SteerholmUp) { $reup = $true; break }; Start-Sleep -Seconds 1 }
    }
    # the replaced holm.exe still runs
    & (Join-Path $env:LOCALAPPDATA 'steerholm\bin\holm.exe') version *> $null
    $binOk = ($LASTEXITCODE -eq 0)
    if ($installRc -eq 0 -and $reup -and $binOk) {
        $updateOk = $true
        Emit "update over running daemon ($env:PLATFORM)" Update passed
    } else {
        Emit "update over running daemon failed (rc=$installRc reup=$reup binOk=$binOk) ($env:PLATFORM)" Update failed
    }
} else {
    Emit "update skipped: daemon not up ($env:PLATFORM)" Update failed
}

# ── Uninstall + removal verification ────────────────────────────────
powershell -ExecutionPolicy Bypass -File scripts/uninstall.ps1
$bin = Join-Path $env:LOCALAPPDATA 'steerholm\bin\holm.exe'
$removed = -not (Test-Path $bin)
if ($removed) { Emit "binary removed ($env:PLATFORM)" Uninstall passed }
else { Emit "binary still present after uninstall ($env:PLATFORM)" Uninstall failed }

if ($up -and $usageOk -and $updateOk -and $removed) { exit 0 } else { exit 1 }
