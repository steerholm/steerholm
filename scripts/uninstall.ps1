$ErrorActionPreference = "Stop"

$TaskName = "Steerholm"
$installDir = Join-Path $env:LOCALAPPDATA "steerholm\bin"

function Info($msg) { Write-Host "[+] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }

# ── 1. Stop and remove the autostart task ──────────────────────────

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Info "Removed autostart task."
}

# Make sure the daemon process has exited before deleting its binary
for ($i = 0; $i -lt 15; $i++) {
    $proc = Get-Process -Name 'holm','holmd' -ErrorAction SilentlyContinue
    if (-not $proc) { break }
    Stop-Process -Name 'holm','holmd' -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

# ── 2. Remove binary ───────────────────────────────────────────────

# Legacy service-binary directory from pre-task installs.
$svcDir = Join-Path $env:LOCALAPPDATA "steerholm\svc"
if (Test-Path $svcDir) { Remove-Item $svcDir -Recurse -Force -ErrorAction SilentlyContinue }

if (Test-Path $installDir) {
    Remove-Item $installDir -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $installDir) {
        Warn "Some files still locked. They will be removed on next reboot."
    } else {
        Info "Removed binary."
    }
}

# ── 3. Remove from PATH ────────────────────────────────────────────

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -and $userPath -like "*$installDir*") {
    $newPath = ($userPath -split ";" | Where-Object { $_ -ne $installDir }) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Info "Removed from PATH."
}

Info "Uninstall complete."
Info "Config files remain at $env:APPDATA\steerholm - delete manually if desired."
