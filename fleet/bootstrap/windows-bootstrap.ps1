# windows-bootstrap.ps1 — 1-click Windows agent onboarding
#
# Usage (run in elevated PowerShell or as a one-liner):
#   $env:AGENT="Brian"; $env:TS_KEY="tskey-auth-xxx"; $env:BOT_TOKEN="MTQ..."; `
#   Set-ExecutionPolicy Bypass -Scope Process -Force; irm https://raw.githubusercontent.com/RollingStonie/MightyOS/main/fleet/bootstrap/windows-bootstrap.ps1 | iex
#
# Required env vars:
#   AGENT      — agent name (Brian, Mike, Lucy, etc.)
#   TS_KEY     — Tailscale auth key (from Tailscale admin console)
#
# Optional env vars:
#   BOT_TOKEN  — Discord bot token for this agent
#   DS_KEY     — DeepSeek API key (default: sk-123dda9dcd104362929f51dc48cb88c1)
#   GH_TOKEN   — GitHub PAT for downloading private repo files (optional)
#   HERMES_RAW — Base raw URL for Hermes scripts (default: RollingStonie/Hermes/main/sync)
#   SKIP_GPU   — set to "1" to skip Ollama install (default: auto-detect)

param()
$ErrorActionPreference = "Stop"

# ─── 0. Self-elevate to admin ────────────────────────────────────────────────
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "[bootstrap] Relaunching as Administrator..."
    $argStr = "-NoProfile -ExecutionPolicy Bypass -Command `"& {`n"
    foreach ($kv in (Get-ChildItem Env: | Where-Object { $_.Name -in @('AGENT','TS_KEY','BOT_TOKEN','DS_KEY','GH_TOKEN') })) {
        $argStr += "`$env:$($kv.Name) = '$($kv.Value)'`n"
    }
    $argStr += "irm 'https://raw.githubusercontent.com/RollingStonie/MightyOS/main/fleet/bootstrap/windows-bootstrap.ps1' | iex}`""
    Start-Process powershell -Verb RunAs -ArgumentList $argStr -Wait
    exit
}

# ─── Config ──────────────────────────────────────────────────────────────────
$AGENT      = if ($env:AGENT)     { $env:AGENT }     else { throw "AGENT env var required" }
$TS_KEY     = if ($env:TS_KEY)    { $env:TS_KEY }    else { throw "TS_KEY env var required" }
$BOT_TOKEN  = if ($env:BOT_TOKEN) { $env:BOT_TOKEN } else { "" }
$DS_KEY     = if ($env:DS_KEY)    { $env:DS_KEY }    else { "sk-123dda9dcd104362929f51dc48cb88c1" }
$GH_TOKEN   = if ($env:GH_TOKEN)  { $env:GH_TOKEN }  else { "" }
$HERMES_RAW = if ($env:HERMES_RAW){ $env:HERMES_RAW } else { "https://raw.githubusercontent.com/RollingStonie/MightyOS/main/sync" }

$HERMES_DIR = "C:\hermes"
$LOG        = "$HERMES_DIR\bootstrap.log"
$PYTHON_CANDIDATES = @("C:\Python314\python.exe","C:\Python313\python.exe","C:\Python312\python.exe","C:\Python311\python.exe","C:\Python310\python.exe")

$KENNETH_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIP6lzwuWHt+zNUu+skUWbR3TYUYSfqRtP39IPO95dN0 kennethworkinfrastructure@gmail.com"

# Fleet webhook — edit after Kenneth adds it to ~/.hermes/.env on VPS
$DISCORD_WEBHOOK = $env:FLEET_WEBHOOK

New-Item -Path $HERMES_DIR -ItemType Directory -Force | Out-Null
"bootstrap started $(Get-Date)" | Tee-Object -FilePath $LOG | Write-Host

function Log { param([string]$msg) $ts = (Get-Date -Format "HH:mm:ss"); "$ts  $msg" | Tee-Object -FilePath $LOG -Append | Write-Host }
function Fetch-File { param([string]$url,[string]$dest)
    $headers = @{}
    if ($GH_TOKEN) { $headers["Authorization"] = "Bearer $GH_TOKEN" }
    try { Invoke-WebRequest -Uri $url -Headers $headers -OutFile $dest -UseBasicParsing -ErrorAction Stop; return $true }
    catch { return $false }
}

# ─── 1. LocalAccountTokenFilterPolicy (required for SSH from remote) ─────────
Log "Setting LocalAccountTokenFilterPolicy..."
New-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" `
    -Name LocalAccountTokenFilterPolicy -Value 1 -PropertyType DWord -Force | Out-Null

# ─── 2. OpenSSH Server ───────────────────────────────────────────────────────
Log "Installing OpenSSH Server..."
$sshdState = (Get-WindowsCapability -Online -Name "OpenSSH.Server*").State
if ($sshdState -ne "Installed") {
    Add-WindowsCapability -Online -Name "OpenSSH.Server~~~~0.0.1.0" | Out-Null
}
Set-Service sshd -StartupType Automatic
Start-Service sshd
New-NetFirewallRule -Name sshd -DisplayName "OpenSSH" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -ErrorAction SilentlyContinue | Out-Null

# Default shell → PowerShell
New-Item -Path "HKLM:\SOFTWARE\OpenSSH" -Force | Out-Null
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell `
    -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force | Out-Null

# Kenneth's SSH key
$authKeys = "$env:ProgramData\ssh\administrators_authorized_keys"
if (-not (Test-Path $authKeys)) { New-Item $authKeys -Force | Out-Null }
if (-not (Select-String -Path $authKeys -Pattern "kennethworkinfrastructure" -Quiet -ErrorAction SilentlyContinue)) {
    Add-Content $authKeys $KENNETH_PUBKEY
}
icacls $authKeys /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F" | Out-Null
Log "OpenSSH ready."

# ─── 3. winget: Python, Node.js, Chrome, Tailscale ───────────────────────────
Log "Installing packages via winget..."
function Winget-Install { param([string]$id,[string]$label)
    Log "  Installing $label..."
    winget install --id $id --silent --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
}

Winget-Install "Tailscale.Tailscale"       "Tailscale"
Winget-Install "Python.Python.3.12"       "Python 3.12"
Winget-Install "OpenJS.NodeJS.LTS"        "Node.js LTS"
Winget-Install "Google.Chrome"            "Google Chrome"
Winget-Install "Discord.Discord"          "Discord"
Winget-Install "SlackTechnologies.Slack"  "Slack"
Winget-Install "Keeper.KeeperDesktop"     "Keeper Password Manager"
Winget-Install "NordVPN.NordVPN"          "NordVPN"

# Optional: Ollama for local LLM (only if GPU detected or SKIP_GPU != "1")
if ($env:SKIP_GPU -ne "1") {
    $gpu = (Get-CimInstance Win32_VideoController | Where-Object { $_.Name -match "NVIDIA|AMD" })
    if ($gpu) {
        Log "  GPU detected ($($gpu.Name)) — installing Ollama..."
        Winget-Install "Ollama.Ollama" "Ollama"
    } else {
        Log "  No GPU detected — skipping Ollama (set DS_KEY for DeepSeek API fallback)"
    }
}

# Refresh PATH so python/node/npm are visible in this session
$env:PATH = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# ─── 4. Find Python ──────────────────────────────────────────────────────────
$PYTHON = $null
foreach ($p in $PYTHON_CANDIDATES) { if (Test-Path $p) { $PYTHON = $p; break } }
if (-not $PYTHON) { $cmd = Get-Command python -ErrorAction SilentlyContinue; if ($cmd) { $PYTHON = $cmd.Source } }
if (-not $PYTHON) { throw "Python not found after install — reboot and re-run" }
Log "Python: $PYTHON"

# ─── 5. pip dependencies ─────────────────────────────────────────────────────
Log "Installing pip packages..."
& $PYTHON -m pip install --quiet psutil aiohttp

# ─── 6. Claude Code CLI + Codex ──────────────────────────────────────────────
Log "Installing Claude Code CLI..."
npm install -g @anthropic-ai/claude-code 2>&1 | Out-Null
Log "Installing Codex..."
npm install -g @openai/codex 2>&1 | Out-Null

# Antigravity tooling — install if found in registry
# (Antigravity-specific npm package or repo clone — extend here when available)
# npm install -g @antigravity/... when package is published

# ─── 7. Tailscale: join fleet network ────────────────────────────────────────
Log "Joining Tailscale fleet network..."
$ts = "C:\Program Files\Tailscale\tailscale.exe"
if (Test-Path $ts) {
    & $ts up --authkey=$TS_KEY --accept-routes --operator="" 2>&1 | Out-Null
} else {
    Log "  WARNING: tailscale.exe not found at expected path — may need a reboot to complete install"
}

# ─── 8. Download agent scripts ───────────────────────────────────────────────
Log "Downloading agent scripts..."

$FILES = @("heartbeat.py","agent_bot.py","lead_watcher.py","twenty_client.py","push_leads_to_twenty.py")
foreach ($f in $FILES) {
    $ok = Fetch-File "$HERMES_RAW/$f" "$HERMES_DIR\$f"
    if ($ok) { Log "  [OK] $f" } else { Log "  [SKIP] $f — not found or no GH_TOKEN" }
}

# ─── 9. Write env files ───────────────────────────────────────────────────────
Log "Writing env files..."

@"
AGENT=$AGENT
ROLE=fleet-agent
FLEET_WEBHOOK=$DISCORD_WEBHOOK
BEAT_SECS=300
"@ | Set-Content "$HERMES_DIR\heartbeat.env" -Encoding UTF8

if ($BOT_TOKEN) {
@"
DISCORD_BOT_TOKEN=$BOT_TOKEN
DEEPSEEK_API_KEY=$DS_KEY
DEEPSEEK_BASE_URL=https://api.deepseek.com
AGENT=$AGENT
AGENT_SYS=You are $AGENT, Kenneth's fleet agent. Be concise and direct.
"@ | Set-Content "$HERMES_DIR\bot.env" -Encoding UTF8
}

# ─── 10. Task Scheduler: heartbeat + bot (infinite-restart loop) ─────────────
Log "Registering scheduled tasks..."
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$pw   = Read-Host "Enter Windows password for '$user' (needed for task persistence)" -AsSecureString
$pwPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))

# Heartbeat — every 5 minutes
$hbAction = New-ScheduledTaskAction `
    -Execute "C:\Windows\System32\cmd.exe" `
    -Argument "/c `"$PYTHON -u $HERMES_DIR\heartbeat.py >> $HERMES_DIR\heartbeat.log 2>&1`"" `
    -WorkingDirectory $HERMES_DIR
$hbTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "HermesAgentHeartbeat" -Action $hbAction -Trigger $hbTrigger `
    -User $user -Password $pwPlain -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName "HermesAgentHeartbeat"
Log "  Task: HermesAgentHeartbeat registered"

# Bot — infinite crash-restart loop
if ($BOT_TOKEN) {
    $botAction = New-ScheduledTaskAction `
        -Execute "C:\Windows\System32\cmd.exe" `
        -Argument "/c `"for /L %i in (1,0,1) do ($PYTHON -u $HERMES_DIR\agent_bot.py >> $HERMES_DIR\bot.log 2>&1 & timeout /t 5 /nobreak > nul)`"" `
        -WorkingDirectory $HERMES_DIR
    $botTrigger = New-ScheduledTaskTrigger -AtStartup
    Register-ScheduledTask -TaskName "HermesAgentBot" -Action $botAction -Trigger $botTrigger `
        -User $user -Password $pwPlain -RunLevel Highest -Force | Out-Null
    Start-ScheduledTask -TaskName "HermesAgentBot"
    Log "  Task: HermesAgentBot registered"
}

# Lead watcher (for scraping agents)
if (Test-Path "$HERMES_DIR\lead_watcher.py") {
    $lwAction = New-ScheduledTaskAction `
        -Execute "C:\Windows\System32\cmd.exe" `
        -Argument "/c `"for /L %i in (1,0,1) do ($PYTHON -u $HERMES_DIR\lead_watcher.py >> $HERMES_DIR\lead_watcher.log 2>&1 & timeout /t 10 /nobreak > nul)`"" `
        -WorkingDirectory $HERMES_DIR
    $lwTrigger = New-ScheduledTaskTrigger -AtStartup
    Register-ScheduledTask -TaskName "HermesLeadWatcher" -Action $lwAction -Trigger $lwTrigger `
        -User $user -Password $pwPlain -RunLevel Highest -Force | Out-Null
    Log "  Task: HermesLeadWatcher registered (not started — start manually when scraper is ready)"
}

# MightyOS Brain repo sync — every 5 min git pull
if ($GH_TOKEN) {
    $syncScript = @"
`$env:GIT_ASKPASS = 'echo'
git -C C:\MightyOS pull --quiet 2>&1 | Out-Null
"@
    $syncScript | Set-Content "$HERMES_DIR\sync_brain.ps1" -Encoding UTF8
    $syncAction = New-ScheduledTaskAction `
        -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -Argument "-NonInteractive -File $HERMES_DIR\sync_brain.ps1"
    $syncTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName "HermesBrainSync" -Action $syncAction -Trigger $syncTrigger `
        -User $user -Password $pwPlain -RunLevel Highest -Force | Out-Null

    # Clone MightyOS Brain repo
    Log "Cloning MightyOS Brain repo..."
    if (-not (Test-Path "C:\MightyOS")) {
        git clone "https://$GH_TOKEN@github.com/RollingStonie/MightyOS.git" "C:\MightyOS" 2>&1 | Out-Null
    }
    Log "  Task: HermesBrainSync registered"
}

# ─── 11. Announce online ─────────────────────────────────────────────────────
if ($DISCORD_WEBHOOK) {
    Log "Posting online announcement to Discord..."
    $body = @{ content = ":white_check_mark: **$AGENT** bootstrap complete — online and ready." } | ConvertTo-Json
    try { Invoke-RestMethod -Uri $DISCORD_WEBHOOK -Method Post -Body $body -ContentType "application/json" | Out-Null }
    catch { Log "  (Discord post failed — set FLEET_WEBHOOK env var later)" }
}

# ─── Done ─────────────────────────────────────────────────────────────────────
Log ""
Log "════════════════════════════════════════════════════════"
Log "  $AGENT bootstrap complete!"
Log "  Python:      $PYTHON"
Log "  Hermes dir:  $HERMES_DIR"
Log "  Tasks:       HermesAgentHeartbeat, HermesAgentBot, HermesLeadWatcher"
Log "  Log:         $LOG"
Log "  Tailscale:   join fleet — verify with: tailscale status"
Log "  Chrome:      installed"
Log "  Discord:     installed"
Log "  Slack:       installed"
Log "  Keeper:      installed (open and log in manually)"
Log "  NordVPN:     installed (open and log in manually)"
Log "  Claude Code: $(npm list -g @anthropic-ai/claude-code --depth 0 2>$null | Select-String 'claude' | Select-Object -First 1)"
Log "  Codex:       $(npm list -g @openai/codex --depth 0 2>$null | Select-String 'codex' | Select-Object -First 1)"
Log ""
Log "  Next steps:"
Log "  1. Copy secrets: cp ~/.hermes/secrets/ from Keeper"
Log "  2. Start lead watcher when scraper ready: schtasks /run /tn HermesLeadWatcher"
Log "  3. Verify in Discord: @$AGENT hello"
Log "════════════════════════════════════════════════════════"

Read-Host "Press Enter to close"
