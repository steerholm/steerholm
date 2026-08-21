$ErrorActionPreference = "Stop"

$Repo = "mcpharbour/mcpharbour"
$TaskName = "MCPHarbour"
$Platform = "windows-x64"
$installDir = Join-Path $env:LOCALAPPDATA "mcp-harbour\bin"

function Info($msg)  { Write-Host "[+] $msg" -ForegroundColor Green }
function Warn($msg)  { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Fail($msg)  { Write-Host "[x] $msg" -ForegroundColor Red; exit 1 }
function Test-Harbour($p) {
    # Confirm it is Harbour answering, not just any process holding the port.
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$p/healthz" -TimeoutSec 1 -ErrorAction Stop
        return ($r.service -eq 'mcp-harbour')
    } catch { return $false }
}
function Install-Binary($src, $destDir, $name) {
    $dest = Join-Path $destDir $name
    if (Test-Path $dest) {
        # Windows can't overwrite a running .exe, but it CAN rename it. Moving the
        # old one aside is what lets `harbour update` replace the very binary that
        # is running the update, without killing the updater process.
        $old = "$dest.old"
        Remove-Item $old -Force -ErrorAction SilentlyContinue
        try { Move-Item $dest $old -Force -ErrorAction Stop } catch {}
    }
    Copy-Item $src $dest -Force
}

if ($HarbourBinaryPath) {
    # ── Local mode: copy from provided path ────────────────────────
    $sourceDir = Split-Path -Parent (Resolve-Path $HarbourBinaryPath).Path
    Info "Copying binary from: $sourceDir"
} elseif ($env:MCP_HARBOUR_LOCAL_ARCHIVE) {
    # ── Local-archive mode (testing): extract a provided .zip ──────
    if (-not (Test-Path $env:MCP_HARBOUR_LOCAL_ARCHIVE)) {
        Fail "Local archive not found: $($env:MCP_HARBOUR_LOCAL_ARCHIVE)"
    }
    Info "Installing from local archive: $($env:MCP_HARBOUR_LOCAL_ARCHIVE)"
    $tmpDir = Join-Path $env:TEMP "mcp-harbour-install"
    if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
    New-Item -ItemType Directory -Path $tmpDir | Out-Null
    Expand-Archive -Path $env:MCP_HARBOUR_LOCAL_ARCHIVE -DestinationPath $tmpDir -Force
    $sourceDir = $tmpDir
} else {
    # ── Download release (pinned or latest) ────────────────────────
    if ($env:MCP_HARBOUR_VERSION) {
        Info "Fetching release $env:MCP_HARBOUR_VERSION..."
        $release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/tags/$($env:MCP_HARBOUR_VERSION)"
    } else {
        Info "Fetching latest release..."
        $release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest"
    }
    $tag = $release.tag_name
    $asset = $release.assets | Where-Object { $_.name -eq "mcp-harbour-$Platform.zip" }

    if (-not $asset) { Fail "No release found for $Platform" }

    $tmpDir = Join-Path $env:TEMP "mcp-harbour-install"
    if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
    New-Item -ItemType Directory -Path $tmpDir | Out-Null

    Info "Downloading $tag..."
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile "$tmpDir\release.zip"

    # ── Verify checksum ────────────────────────────────────────────
    $checksumUrl = "https://github.com/$Repo/releases/download/$tag/checksums.txt"
    $checksums = $null
    try {
        # For an octet-stream response, .Content is a byte[]; decode to text so the
        # per-asset lines parse (otherwise every lookup reports "no entry").
        $raw = (Invoke-WebRequest -Uri $checksumUrl -UseBasicParsing).Content
        $checksums = if ($raw -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($raw) } else { $raw }
    } catch {
        Warn "checksums.txt not available for $tag; skipping verification"
    }
    if ($checksums) {
        $assetName = "mcp-harbour-$Platform.zip"
        $line = $checksums -split "`n" | Where-Object { $_ -match [regex]::Escape($assetName) } | Select-Object -First 1
        if (-not $line) { Fail "checksums.txt has no entry for $assetName" }
        $expected = (($line -split '\s+') | Where-Object { $_ })[0].ToLower()
        $actual = (Get-FileHash -Algorithm SHA256 "$tmpDir\release.zip").Hash.ToLower()
        if ($expected -ne $actual) { Fail "Checksum verification failed for $assetName" }
        Info "Checksum verified"
    }

    Expand-Archive -Path "$tmpDir\release.zip" -DestinationPath $tmpDir -Force

    $sourceDir = $tmpDir
}

# ── Install binary to standard location ────────────────────────────

if (-not (Test-Path $installDir)) { New-Item -ItemType Directory -Path $installDir | Out-Null }

# Stop the running daemon (harbourd) so its binary isn't locked during copy.
# Do NOT kill 'harbour': during `harbour update` the running updater IS harbour.exe,
# and killing it would abort the update. The CLI is replaced via rename-then-copy.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
for ($i = 0; $i -lt 15; $i++) {
    if (-not (Get-Process -Name 'harbourd' -ErrorAction SilentlyContinue)) { break }
    Stop-Process -Name 'harbourd' -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
}

# If something still serves on 4767 (a stray daemon, or a foreground
# `harbour serve`), stop that specific process by PID — never by image name, so
# we never kill the running updater (`harbour update`), which does not hold 4767.
if (Test-Harbour 4767) {
    try {
        $owners = (Get-NetTCPConnection -LocalPort 4767 -State Listen -ErrorAction SilentlyContinue).OwningProcess
        foreach ($procId in ($owners | Sort-Object -Unique)) {
            if ($procId -and $procId -ne $PID) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
        }
    } catch {}
}

Install-Binary (Join-Path $sourceDir "harbour.exe") $installDir "harbour.exe"

$daemonSource = Join-Path $sourceDir "harbourd.exe"
if (Test-Path $daemonSource) { Install-Binary $daemonSource $installDir "harbourd.exe" }

# Sweep leftover *.old images from prior self-updates (the current update's
# harbour.exe.old is still locked by the running updater and is cleared next run).
Remove-Item (Join-Path $installDir "*.old") -Force -ErrorAction SilentlyContinue

if ($tmpDir -and (Test-Path $tmpDir)) { Remove-Item $tmpDir -Recurse -Force }

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$installDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$installDir;$userPath", "User")
    $env:Path = "$installDir;$env:Path"
    Info "Added $installDir to PATH"
}

$HarbourBin = Join-Path $installDir "harbour.exe"
$DaemonBin = Join-Path $installDir "harbourd.exe"
Info "Installed binary to $installDir"

# ── Register a per-user autostart (logon Scheduled Task) ───────────
# Runs `harbour serve` as the current user in their own session — no admin,
# no stored password. This is the Windows mirror of the systemd --user unit
# (Linux) and the LaunchAgent (macOS).

if ($env:MCP_HARBOUR_NO_SERVICE) {
    Info "Skipping autostart registration (MCP_HARBOUR_NO_SERVICE set)."
    Info "Run the daemon manually with: harbour serve"
    Write-Host ""
    Info "Installation complete."
    exit 0
}

$logDir = Join-Path $env:APPDATA "mcp-harbour"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

try {
    # Prefer the windowless daemon (no console window); fall back to the CLI.
    if (Test-Path $DaemonBin) {
        $action = New-ScheduledTaskAction -Execute $DaemonBin
    } else {
        $action = New-ScheduledTaskAction -Execute $HarbourBin -Argument "serve"
    }
    $account   = "$env:USERDOMAIN\$env:USERNAME"
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $account
    $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
                    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName

    # /Run is fire-and-forget; confirm the daemon actually bound the port.
    $up = $false
    for ($i = 0; $i -lt 20; $i++) {
        if (Test-Harbour 4767) { $up = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if ($up) {
        Info "Registered logon task; daemon running on 127.0.0.1:4767"
    } else {
        Warn "Registered logon task, but the daemon is not listening on 127.0.0.1:4767 yet."
        Warn "On a headless/non-interactive session it starts at your next logon, or run: harbour start"
    }
} catch {
    Warn "Could not register the autostart task: $($_.Exception.Message)"
    Warn "You can run the daemon manually: harbour serve"
}

Write-Host ""
Info "Manage with:"
Write-Host "  harbour status"
Write-Host "  harbour stop"
Write-Host "  harbour start"

Write-Host ""
Info "Installation complete."
