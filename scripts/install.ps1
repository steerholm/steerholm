$ErrorActionPreference = "Stop"

# Run by `holm update` from 0.1.0 or 0.1.1, this script inherits PyInstaller's
# bookkeeping from that frozen binary. With it, the NEW `holm migrate` below
# takes itself for a child of the old `holm update` and loads the old build's
# unpacked libraries instead of its own. Later releases start it without.
# Which file that updater reads its modules from is kept first — see
# Stop-OldUpdater.
$oldUpdaterArchive = $env:_PYI_ARCHIVE_FILE
Get-ChildItem Env: | Where-Object { $_.Name -like '_PYI_*' } |
    ForEach-Object { Remove-Item "Env:$($_.Name)" }

$Repo = "steerholm/steerholm"
$TaskName = "Steerholm"
$Platform = "windows-x64"
$installDir = Join-Path $env:LOCALAPPDATA "steerholm\bin"

function Info($msg)  { Write-Host "[+] $msg" -ForegroundColor Green }
function Warn($msg)  { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Fail($msg)  { Write-Host "[x] $msg" -ForegroundColor Red; Exit-Installer 1 }
# Ends the install with `code` — by throwing, not `exit`: see the end of this file.
function Exit-Installer([int]$code) {
    $e = [System.Exception]::new("Steerholm installer exit $code")
    $e.Data["SteerholmExit"] = $code
    throw $e
}
# `holm update` from 0.1.0 or 0.1.1 is a frozen binary that reads each Python
# module from its own file, by path, the first time it imports it. Once this
# script swaps holm.exe, the updater's next import — printing its last line, or
# reporting an error — reads the NEW build at the old one's offsets and dies
# with zlib.error. So that nothing of the old binary runs once the new one is in
# place, this script reports for it and ends it, with the exit code it would
# have had, as its own last act.
#
# 0.1.2 and later strip PyInstaller's variables before running the installer and
# exit cleanly on their own, so this only ever matches an update started from
# 0.1.0 or 0.1.1. Kept for as long as anyone may update from those.
$oldUpdater = $null
if ($oldUpdaterArchive -and
    [string]::Equals([IO.Path]::GetFullPath($oldUpdaterArchive),
                     [IO.Path]::GetFullPath((Join-Path $installDir "holm.exe")),
                     [StringComparison]::OrdinalIgnoreCase)) {
    # Best effort: failing to find it must not stop the install itself.
    try { $oldUpdater = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction Stop).ParentProcessId }
    catch { $oldUpdater = $null }
}
$binarySwapped = $false
function Stop-OldUpdater([int]$code) {
    if (-not $oldUpdater -or -not $binarySwapped) { return }
    # Once, whatever happens below.
    $script:oldUpdater = $null
    if ($code -eq 0) { Write-Host "Updated Steerholm to $(if ($tag) { $tag } else { 'the new version' })." }
    # Its temporary directory, which it would have removed; it holds this script.
    $own = Split-Path -Parent $PSCommandPath
    if ((Split-Path -Leaf $own) -like 'steerholm-update-*') {
        Remove-Item $own -Recurse -Force -ErrorAction SilentlyContinue
    }
    $target = [int]$oldUpdater
    try {
        # TerminateProcess takes the exit code, so a successful update still
        # reports 0. Add-Type compiles C#, which some locked-down machines refuse.
        Add-Type -Namespace Steerholm -Name Native -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)] public static extern System.IntPtr OpenProcess(uint access, bool inherit, int pid);
[DllImport("kernel32.dll", SetLastError = true)] public static extern bool TerminateProcess(System.IntPtr handle, uint code);
[DllImport("kernel32.dll")] public static extern bool CloseHandle(System.IntPtr handle);
'@
        $handle = [Steerholm.Native]::OpenProcess(0x0001, $false, $target)  # PROCESS_TERMINATE
        if ($handle -eq [IntPtr]::Zero) { throw "OpenProcess failed" }
        [void][Steerholm.Native]::TerminateProcess($handle, [uint32]$code)
        [void][Steerholm.Native]::CloseHandle($handle)
    } catch {
        # Still end it — nothing of the old binary may run — at the cost of a
        # non-zero exit code.
        Stop-Process -Id $target -Force -ErrorAction SilentlyContinue
    }
}

function Test-Steerholm($p) {
    # Confirm it is Steerholm answering, not just any process holding the port.
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$p/healthz" -TimeoutSec 1 -ErrorAction Stop
        return ($r.service -eq 'steerholm')
    } catch { return $false }
}
function Install-Binary($src, $destDir, $name) {
    $dest = Join-Path $destDir $name
    if (Test-Path $dest) {
        # Windows can't overwrite a running .exe, but it CAN rename it. Moving the
        # old one aside is what lets `holm update` replace the very binary that
        # is running the update, without killing the updater process.
        $old = "$dest.old"
        Remove-Item $old -Force -ErrorAction SilentlyContinue
        try { Move-Item $dest $old -Force -ErrorAction Stop } catch {}
    }
    Copy-Item $src $dest -Force
}

$exitCode = 0
try {
    if ($SteerholmBinaryPath) {
        # ── Local mode: copy from provided path ────────────────────────
        $sourceDir = Split-Path -Parent (Resolve-Path $SteerholmBinaryPath).Path
        Info "Copying binary from: $sourceDir"
    } elseif ($env:STEERHOLM_LOCAL_ARCHIVE) {
        # ── Local-archive mode (testing): extract a provided .zip ──────
        if (-not (Test-Path $env:STEERHOLM_LOCAL_ARCHIVE)) {
            Fail "Local archive not found: $($env:STEERHOLM_LOCAL_ARCHIVE)"
        }
        Info "Installing from local archive: $($env:STEERHOLM_LOCAL_ARCHIVE)"
        $tmpDir = Join-Path $env:TEMP "steerholm-install"
        if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
        New-Item -ItemType Directory -Path $tmpDir | Out-Null
        Expand-Archive -Path $env:STEERHOLM_LOCAL_ARCHIVE -DestinationPath $tmpDir -Force
        $sourceDir = $tmpDir
    } else {
        # ── Download release (pinned or latest) ────────────────────────
        if ($env:STEERHOLM_VERSION) {
            Info "Fetching release $env:STEERHOLM_VERSION..."
            $release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/tags/$($env:STEERHOLM_VERSION)"
        } else {
            Info "Fetching latest release..."
            $release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest"
        }
        $tag = $release.tag_name
        $asset = $release.assets | Where-Object { $_.name -eq "steerholm-$Platform.zip" }

        if (-not $asset) { Fail "No release found for $Platform" }

        $tmpDir = Join-Path $env:TEMP "steerholm-install"
        if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
        New-Item -ItemType Directory -Path $tmpDir | Out-Null

        Info "Downloading $tag..."
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile "$tmpDir\release.zip"

        # ── Verify checksum ────────────────────────────────────────────
        $checksumUrl = "https://github.com/$Repo/releases/download/$tag/checksums.txt"
        # Every release publishes checksums.txt, so a missing one means its download
        # failed or was blocked — exactly when nothing should be installed unverified.
        # Nothing has been stopped or replaced yet.
        try {
            # For an octet-stream response, .Content is a byte[]; decode to text so the
            # per-asset lines parse (otherwise every lookup reports "no entry").
            $raw = (Invoke-WebRequest -Uri $checksumUrl -UseBasicParsing).Content
            $checksums = if ($raw -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($raw) } else { $raw }
        } catch {
            Fail "Could not download checksums.txt for $tag, so the download cannot be verified. Nothing was installed; try again."
        }
        $assetName = "steerholm-$Platform.zip"
        $line = $checksums -split "`n" | Where-Object { $_ -match [regex]::Escape($assetName) } | Select-Object -First 1
        if (-not $line) { Fail "checksums.txt has no entry for $assetName" }
        $expected = (($line -split '\s+') | Where-Object { $_ })[0].ToLower()
        $actual = (Get-FileHash -Algorithm SHA256 "$tmpDir\release.zip").Hash.ToLower()
        if ($expected -ne $actual) { Fail "Checksum verification failed for $assetName" }
        Info "Checksum verified"

        Expand-Archive -Path "$tmpDir\release.zip" -DestinationPath $tmpDir -Force

        $sourceDir = $tmpDir
    }

    # ── Install binary to standard location ────────────────────────────

    if (-not (Test-Path $installDir)) { New-Item -ItemType Directory -Path $installDir | Out-Null }

    # Stop the running daemon (holmd) so its binary isn't locked during copy.
    # Do NOT kill 'holm': during `holm update` the running updater IS holm.exe,
    # and killing it would abort the update. The CLI is replaced via rename-then-copy.
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    for ($i = 0; $i -lt 15; $i++) {
        if (-not (Get-Process -Name 'holmd' -ErrorAction SilentlyContinue)) { break }
        Stop-Process -Name 'holmd' -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }

    # If something still serves on 4767 (a stray daemon, or a foreground
    # `holm serve`), stop that specific process by PID — never by image name, so
    # we never kill the running updater (`holm update`), which does not hold 4767.
    if (Test-Steerholm 4767) {
        try {
            $owners = (Get-NetTCPConnection -LocalPort 4767 -State Listen -ErrorAction SilentlyContinue).OwningProcess
            foreach ($procId in ($owners | Sort-Object -Unique)) {
                if ($procId -and $procId -ne $PID) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
            }
        } catch {}
    }

    Install-Binary (Join-Path $sourceDir "holm.exe") $installDir "holm.exe"
    $binarySwapped = $true

    $daemonSource = Join-Path $sourceDir "holmd.exe"
    if (Test-Path $daemonSource) { Install-Binary $daemonSource $installDir "holmd.exe" }

    # ── Migrate the on-disk state ──────────────────────────────────────

    # Run by the NEWLY installed binary, not the one that started this script: the
    # process running `holm update` is the old version and cannot contain the new
    # schema steps. The daemon was stopped above and is not registered again until
    # later, so nothing is reading or writing the state while this runs.
    $holmExe = Join-Path $installDir "holm.exe"
    # PowerShell 7.4+ turns $PSNativeCommandUseErrorActionPreference on by default,
    # which combines with the $ErrorActionPreference="Stop" at the top of this file
    # to make a non-zero exit from a native command throw. That would abort the
    # script here and print a PowerShell error record instead of the message below.
    # On Windows PowerShell 5.1 the variable does not exist and $LASTEXITCODE alone
    # is already correct, so this is a no-op there.
    $prevNative = $null
    if (Test-Path Variable:PSNativeCommandUseErrorActionPreference) {
        $prevNative = $PSNativeCommandUseErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $false
    }
    & $holmExe migrate
    $migrateExit = $LASTEXITCODE
    if ($null -ne $prevNative) { $PSNativeCommandUseErrorActionPreference = $prevNative }

    if ($migrateExit -ne 0) {
        Write-Host "[x] Could not migrate the Steerholm state." -ForegroundColor Red
        Write-Host "    The new binary is installed but the state was not migrated," -ForegroundColor Red
        Write-Host "    so the daemon will refuse to start against it. Fix the cause" -ForegroundColor Red
        Write-Host "    above, then re-run this installer." -ForegroundColor Red
        if ($tmpDir -and (Test-Path $tmpDir)) { Remove-Item $tmpDir -Recurse -Force }
        Exit-Installer 1
    }

    # Sweep leftover *.old images from prior self-updates (the current update's
    # holm.exe.old is still locked by the running updater and is cleared next run).
    Remove-Item (Join-Path $installDir "*.old") -Force -ErrorAction SilentlyContinue

    if ($tmpDir -and (Test-Path $tmpDir)) { Remove-Item $tmpDir -Recurse -Force }

    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$installDir*") {
        [Environment]::SetEnvironmentVariable("Path", "$installDir;$userPath", "User")
        $env:Path = "$installDir;$env:Path"
        Info "Added $installDir to PATH"
    }

    $SteerholmBin = Join-Path $installDir "holm.exe"
    $DaemonBin = Join-Path $installDir "holmd.exe"
    Info "Installed binary to $installDir"

    # ── Register a per-user autostart (logon Scheduled Task) ───────────
    # Runs `holm serve` as the current user in their own session — no admin,
    # no stored password. This is the Windows mirror of the systemd --user unit
    # (Linux) and the LaunchAgent (macOS).

    if ($env:STEERHOLM_NO_SERVICE) {
        Info "Skipping autostart registration (STEERHOLM_NO_SERVICE set)."
        Info "Run the daemon manually with: holm serve"
        Write-Host ""
        Info "Installation complete."
        Exit-Installer 0
    }

    $logDir = Join-Path $env:APPDATA "steerholm"
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

    try {
        # Prefer the windowless daemon (no console window); fall back to the CLI.
        if (Test-Path $DaemonBin) {
            $action = New-ScheduledTaskAction -Execute $DaemonBin
        } else {
            $action = New-ScheduledTaskAction -Execute $SteerholmBin -Argument "serve"
        }
        # From the logon token, not $env:USERDOMAIN: over OpenSSH that reads
        # WORKGROUP, and registering for WORKGROUP\<user> fails with "No mapping
        # between account names and security IDs".
        $account   = [Security.Principal.WindowsIdentity]::GetCurrent().Name
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
            if (Test-Steerholm 4767) { $up = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if ($up) {
            Info "Registered logon task; daemon running on 127.0.0.1:4767"
        } else {
            Warn "Registered logon task, but the daemon is not listening on 127.0.0.1:4767 yet."
            # The task runs only in a signed-in Windows session; an SSH session is not
            # one, so `holm start` from it cannot start the task either.
            Warn "It runs only while you are signed in to Windows (at the console or over Remote Desktop),"
            Warn "not from an SSH session. It starts at your next sign-in, or run 'holm start' from such a session."
        }
    } catch {
        Warn "Could not register the autostart task: $($_.Exception.Message)"
        Warn "You can run the daemon manually: holm serve"
    }

    Write-Host ""
    Info "Manage with:"
    Write-Host "  holm status"
    Write-Host "  holm stop"
    Write-Host "  holm start"

    Write-Host ""
    Info "Installation complete."
} catch {
    $failure = $_
    $signalled = $failure.Exception.Data["SteerholmExit"]
    if ($null -ne $signalled) {
        $exitCode = $signalled
    } else {
        $exitCode = 1
        $status = $null
        try { $status = [int]$failure.Exception.Response.StatusCode } catch {}
        if ($status) {
            # A server that refused: its own explanation is in the body
            # (ErrorDetails), not in the exception's "(403) Forbidden".
            # Only a JSON `message`: a proxy's HTML error page is not a reason.
            $reason = $null
            if ($failure.ErrorDetails -and $failure.ErrorDetails.Message) {
                try { $reason = ($failure.ErrorDetails.Message | ConvertFrom-Json).message } catch {}
            }
            if ($reason) { $reason = ($reason -split ' \(', 2)[0].TrimEnd('.') }   # up to any parenthetical aside
            Write-Host "[x] The release server answered $status$(if ($reason) { ": $reason" }). Try again later." -ForegroundColor Red
        } else {
            Write-Host "[x] $($failure.Exception.Message) (install.ps1 line $($failure.InvocationInfo.ScriptLineNumber))" -ForegroundColor Red
        }
    }
}

# Every way out after the swap, success or not, ends an old updater too.
Stop-OldUpdater $exitCode

# Run as a file — `holm update`, `powershell -File` — the exit code is the
# result, and the updater reads it. Run as `irm ... | iex`, the script is part of
# the user's own session: `exit` would close their window, and the message
# explaining what went wrong with it.
if ($PSCommandPath) { exit $exitCode }
$global:LASTEXITCODE = $exitCode
