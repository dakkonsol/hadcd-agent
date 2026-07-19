<#
.SYNOPSIS
    Install the HADCD node agent as a Windows service via NSSM.

.DESCRIPTION
    Run from an elevated PowerShell prompt (Administrator). Expects
    the agent code installed at $InstallDir, a venv at
    $InstallDir\.venv, and an env file at $EnvFile. If NSSM is not
    on PATH the script downloads the official release and uses it
    locally (no system-wide install required).

    See agent\deploy\windows\README.md for the prerequisite layout
    and an end-to-end walkthrough.

.PARAMETER ServiceName
    The Windows service name. Defaults to "hadcd-agent".

.PARAMETER InstallDir
    Where the agent source lives (must contain agent\ and
    hadcd_workloads\). Defaults to "$env:ProgramFiles\hadcd-agent".

.PARAMETER EnvFile
    Path to the env file (NSSM is told to load these vars into the
    service). Defaults to "$env:ProgramData\hadcd-agent\agent.env".

.PARAMETER StateDir
    Where the agent writes state.json and (file BMS) bms.json.
    Defaults to "$env:ProgramData\hadcd-agent".

.PARAMETER NssmExe
    Path to nssm.exe. If empty, the script tries `nssm` on PATH,
    then downloads NSSM 2.24 to a local cache.

.EXAMPLE
    .\install-service.ps1
    Installs with all defaults.

.EXAMPLE
    .\install-service.ps1 -InstallDir 'D:\hadcd-agent' -ServiceName 'hadcd-agent-staging'
#>
[CmdletBinding()]
param(
    [string] $ServiceName = "hadcd-agent",
    [string] $InstallDir  = (Join-Path $env:ProgramFiles "hadcd-agent"),
    [string] $EnvFile     = (Join-Path $env:ProgramData "hadcd-agent\agent.env"),
    [string] $StateDir    = (Join-Path $env:ProgramData "hadcd-agent"),
    [string] $NssmExe     = ""
)

$ErrorActionPreference = "Stop"

# --- Preconditions ---------------------------------------------------

if (-not ([Security.Principal.WindowsPrincipal]`
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated (Administrator) PowerShell prompt."
}

$pythonExe = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Python venv not found at $pythonExe. Create it with:`n  python -m venv `"$InstallDir\.venv`"`n  $InstallDir\.venv\Scripts\pip install -r $InstallDir\agent\requirements.txt"
}

$agentPkg = Join-Path $InstallDir "agent"
if (-not (Test-Path $agentPkg)) {
    throw "Agent package not found at $agentPkg. Copy the repo's 'agent' and 'hadcd_workloads' directories under $InstallDir."
}

if (-not (Test-Path $EnvFile)) {
    throw "Env file not found at $EnvFile. Copy agent\config.env.example there and edit the four required values."
}

New-Item -ItemType Directory -Force $StateDir | Out-Null
$logsDir = Join-Path $StateDir "logs"
New-Item -ItemType Directory -Force $logsDir | Out-Null

# --- Sunshine companion check (Phase 10g) ----------------------------
# Sunshine is HADCD's recommended companion for interactive use: it
# lets you stream this machine's desktop/GPU to a laptop (via the
# Moonlight client) for gaming or live AI apps, AND it gives the agent
# a way to detect "the user is using this machine right now" so that
# fill-tier work (mining, synthetic heat-fill) pauses instead of
# fighting you for the GPU. We surface it loudly here rather than
# burying it in docs.

function Test-SunshineInstalled {
    if (Get-Command sunshine -ErrorAction SilentlyContinue) { return $true }
    $candidates = @(
        (Join-Path $env:ProgramFiles "Sunshine\sunshine.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Sunshine\sunshine.exe")
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $true }
    }
    # Sunshine also commonly registers a Windows service named "Sunshine".
    if (Get-Service -Name "SunshineService" -ErrorAction SilentlyContinue) { return $true }
    if (Get-Service -Name "Sunshine" -ErrorAction SilentlyContinue) { return $true }
    return $false
}

if (Test-SunshineInstalled) {
    Write-Host "Sunshine detected on this host. Good — Mode 2 (remote desktop) and session-aware fill-tier pausing are available." -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Yellow
    Write-Host "  Sunshine is NOT installed on this machine." -ForegroundColor Yellow
    Write-Host "============================================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  HADCD strongly recommends Sunshine. It gives you:"
    Write-Host "    * Remote desktop / game streaming from your laptop"
    Write-Host "      (you run Moonlight on the laptop, the heater does the work)"
    Write-Host "    * Automatic pausing of background mining / heat-fill while"
    Write-Host "      you are using the machine, so it never fights you for the GPU."
    Write-Host ""
    Write-Host "  Install it (free, open source) from:"
    Write-Host "    https://github.com/LizardByte/Sunshine/releases" -ForegroundColor Cyan
    Write-Host "  Then re-run this installer, or set SESSION_SOURCE=sunshine in"
    Write-Host "  your agent.env once Sunshine is running."
    Write-Host ""
    Write-Host "  See agent\deploy\..\..\docs\sunshine-setup.md for the full walkthrough."
    Write-Host ""
    $answer = Read-Host "Continue installing the HADCD agent WITHOUT Sunshine? [y/N]"
    if ($answer -notmatch '^(y|yes)$') {
        Write-Host "Aborting so you can install Sunshine first. Re-run this script afterward." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "Proceeding without Sunshine. Fill-tier work will run un-paused; install Sunshine later to fix that." -ForegroundColor Yellow
}

# --- Tailscale companion check ---------------------------------------
# Tailscale is needed if you want agents at a different site (e.g. an
# apartment) to phone home to this backend, or if you want to manage
# this node remotely without opening ports. Single-node local setups
# work fine without it, so we nudge rather than block.

function Test-TailscaleInstalled {
    if (Get-Command tailscale -ErrorAction SilentlyContinue) { return $true }
    $candidates = @(
        (Join-Path $env:ProgramFiles "Tailscale\tailscale.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Tailscale\tailscale.exe"),
        (Join-Path $env:LOCALAPPDATA "Tailscale\tailscale.exe")
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $true }
    }
    return $false
}

if (Test-TailscaleInstalled) {
    Write-Host "Tailscale detected on this host. Good — remote agents can reach this backend via its MagicDNS hostname." -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "------------------------------------------------------------" -ForegroundColor Cyan
    Write-Host "  Tailscale is NOT installed on this machine." -ForegroundColor Cyan
    Write-Host "------------------------------------------------------------" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  If you only run HADCD on a single local network, you can"
    Write-Host "  skip Tailscale — the agent and backend talk over LAN."
    Write-Host ""
    Write-Host "  If you want agents in another building (apartment, office,"
    Write-Host "  second site) to connect back to this backend WITHOUT port"
    Write-Host "  forwarding or a public IP, install Tailscale first:"
    Write-Host "    https://tailscale.com/download" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  After installing, set HADCD_API in your agent.env to:"
    Write-Host "    http://<this-machine>.tail-XXXXX.ts.net:8000"
    Write-Host ""
    $tsAnswer = Read-Host "Continue installing without Tailscale? [Y/n]"
    if ($tsAnswer -match '^(n|no)$') {
        Write-Host "Aborting so you can install Tailscale first. Re-run this script afterward." -ForegroundColor Cyan
        exit 1
    }
    Write-Host "Proceeding without Tailscale. Install it later for multi-site support." -ForegroundColor Cyan
}

# --- Locate NSSM -----------------------------------------------------

function Resolve-Nssm {
    param([string] $Explicit)

    if ($Explicit) {
        if (-not (Test-Path $Explicit)) {
            throw "NssmExe '$Explicit' does not exist."
        }
        return (Resolve-Path $Explicit).Path
    }

    $onPath = Get-Command nssm -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }

    # Download NSSM 2.24 once into a cache under StateDir\nssm\.
    $cacheDir = Join-Path $StateDir "nssm"
    $cachedNssm = Join-Path $cacheDir "win64\nssm.exe"
    if (Test-Path $cachedNssm) { return $cachedNssm }

    Write-Host "NSSM not found on PATH; downloading NSSM 2.24..."
    New-Item -ItemType Directory -Force $cacheDir | Out-Null
    $zipPath = Join-Path $cacheDir "nssm-2.24.zip"
    # Authoritative release archive from nssm.cc.
    Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" `
        -OutFile $zipPath -UseBasicParsing
    Expand-Archive -Path $zipPath -DestinationPath $cacheDir -Force
    $extracted = Join-Path $cacheDir "nssm-2.24\win64\nssm.exe"
    if (-not (Test-Path $extracted)) {
        throw "Downloaded NSSM archive did not contain the expected nssm.exe."
    }
    # Move into a stable location so re-runs find it.
    New-Item -ItemType Directory -Force (Split-Path $cachedNssm) | Out-Null
    Move-Item $extracted $cachedNssm -Force
    Remove-Item (Join-Path $cacheDir "nssm-2.24") -Recurse -Force
    Remove-Item $zipPath -Force
    return $cachedNssm
}

$nssm = Resolve-Nssm -Explicit $NssmExe
Write-Host "Using NSSM: $nssm"

# --- (Re-)install the service ----------------------------------------

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Service '$ServiceName' exists -- stopping and removing first."
    & $nssm stop $ServiceName confirm | Out-Null
    & $nssm remove $ServiceName confirm | Out-Null
}

& $nssm install $ServiceName $pythonExe "-m" "agent" "run"
& $nssm set $ServiceName AppDirectory       $InstallDir
& $nssm set $ServiceName DisplayName        "HADCD Node Agent"
& $nssm set $ServiceName Description        "Enrols with the HADCD central server and runs offloaded compute tasks for waste-heat reuse."
& $nssm set $ServiceName Start              SERVICE_AUTO_START
& $nssm set $ServiceName AppStdout          (Join-Path $logsDir "agent.out.log")
& $nssm set $ServiceName AppStderr          (Join-Path $logsDir "agent.err.log")
& $nssm set $ServiceName AppRotateFiles     1
& $nssm set $ServiceName AppRotateBytes     10485760  # 10 MiB per file
& $nssm set $ServiceName AppRotateOnline    1
# Restart policy: throttle to one restart per 5s, then back off.
& $nssm set $ServiceName AppExit            Default Restart
& $nssm set $ServiceName AppRestartDelay    5000
& $nssm set $ServiceName AppThrottle        5000

# Load env vars from the env file. NSSM's AppEnvironmentExtra
# expects a multi-string of KEY=VALUE entries - read the file and
# convert ignoring comments and blank lines.
$envEntries = Get-Content -LiteralPath $EnvFile |
    Where-Object { $_ -and -not ($_ -match '^\s*#') } |
    ForEach-Object { $_.TrimEnd() }
& $nssm set $ServiceName AppEnvironmentExtra $envEntries

# Run as LocalSystem by default (writes under %ProgramData% are fine).
# To run as a dedicated service account, uncomment and provide creds:
# & $nssm set $ServiceName ObjectName "NT SERVICE\$ServiceName"

Write-Host ""
Write-Host "Service '$ServiceName' registered."
Write-Host "Starting it now..."
Start-Service -Name $ServiceName
Start-Sleep -Seconds 2
Get-Service -Name $ServiceName | Format-Table -AutoSize

Write-Host ""
Write-Host "Logs:"
Write-Host "  $logsDir\agent.out.log"
Write-Host "  $logsDir\agent.err.log"
Write-Host ""
Write-Host "Tail with:"
Write-Host "  Get-Content -Wait -Tail 50 '$logsDir\agent.out.log'"
