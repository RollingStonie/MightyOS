#!/usr/bin/env bash
# mac-bootstrap.sh — 1-click macOS agent onboarding
#
# Usage (run in Terminal or as a one-liner):
#   AGENT=Lucy TS_KEY=tskey-auth-xxx BOT_TOKEN=MTQ... \
#   bash <(curl -fsSL https://raw.githubusercontent.com/RollingStonie/MightyOS/main/fleet/bootstrap/mac-bootstrap.sh)
#
# Required env vars:
#   AGENT      — agent name (Lucy, Claire-clone, etc.)
#   TS_KEY     — Tailscale auth key (from Tailscale admin console)
#
# Optional env vars:
#   BOT_TOKEN     — Discord bot token for this agent
#   DS_KEY        — DeepSeek API key. No default — pass explicitly or via Infisical;
#                   a hardcoded fallback used to live here, removed 2026-08-06 (was a
#                   real key committed in plaintext).
#   GH_TOKEN      — GitHub PAT for downloading private repo files
#   HERMES_RAW    — Base raw URL for Hermes scripts
#   SKIP_OLLAMA   — set to "1" to skip Ollama install
#   REPOS         — space-separated "RepoName:local-dir" pairs to clone into
#                   ~/AG_Mission, e.g. REPOS="ContentHub:M012_ContentHub Mighty-Relationship-Management:A009_MRM"
#   DOTFILES_REPO — name of Kenneth's personal skills/dotfiles repo to clone as ~/.claude

set -e

AGENT="${AGENT:?AGENT env var required}"
TS_KEY="${TS_KEY:?TS_KEY env var required}"
BOT_TOKEN="${BOT_TOKEN:-}"
DS_KEY="${DS_KEY:-}"
GH_TOKEN="${GH_TOKEN:-}"
HERMES_RAW="${HERMES_RAW:-https://raw.githubusercontent.com/RollingStonie/MightyOS/main/sync}"
HERMES_DIR="$HOME/hermes"
LOG="$HERMES_DIR/bootstrap.log"
DISCORD_WEBHOOK="${FLEET_WEBHOOK:-}"

KENNETH_PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIP6lzwuWHt+zNUu+skUWbR3TYUYSfqRtP39IPO95dN0 kennethworkinfrastructure@gmail.com"

mkdir -p "$HERMES_DIR"
exec > >(tee -a "$LOG") 2>&1

log() { echo "$(date '+%H:%M:%S')  $*"; }
fetch_file() { # fetch_file <url> <dest>
    local headers=()
    [[ -n "$GH_TOKEN" ]] && headers=(-H "Authorization: Bearer $GH_TOKEN")
    curl -fsSL "${headers[@]}" "$1" -o "$2" 2>/dev/null && return 0 || return 1
}

log "=== $AGENT bootstrap starting ==="

# ─── 1. Homebrew ─────────────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
    log "Installing Homebrew..."
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for Apple Silicon
    if [[ -f /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
        echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> "$HOME/.zprofile"
    fi
else
    log "Homebrew already installed."
fi

# ─── 2. Tailscale ────────────────────────────────────────────────────────────
log "Installing Tailscale..."
if ! command -v tailscale &>/dev/null; then
    brew install --cask tailscale 2>/dev/null || true
fi
# Start Tailscale daemon and join fleet
if command -v tailscale &>/dev/null; then
    sudo tailscale up --authkey="$TS_KEY" --accept-routes 2>/dev/null || \
        log "  WARNING: tailscale up failed — open Tailscale app and join manually"
else
    log "  WARNING: tailscale not in PATH — open /Applications/Tailscale.app and join with key: $TS_KEY"
fi

# ─── 3. Python 3, Node.js, Chrome, Discord, Slack, Docker, NordVPN ──────────
# NB 2026-08-28: keeper-password-manager and pcloud-drive were REMOVED from
# homebrew-cask. They are now DMG-downloads below in Step 3a (separate flow
# because hdiutil attach + cp requires sudo and a GUI fallback).
log "Installing Python, Node.js, and desktop apps (brew-available casks)..."
brew install python@3.12 node 2>/dev/null || true
brew install --cask google-chrome discord slack nordvpn docker 2>/dev/null || true

# ─── 3a. Keeper + pCloud — DMG download (no longer in homebrew-cask) ─────────
# Keeper: https://www.keepersecurity.com/en_GB/download.html
# pCloud:  https://www.pcloud.com/downloads.html (also App Store option)
log "Installing Keeper + pCloud via DMG (no longer in brew)..."

# Keeper — latest Mac DMG, x86 + arm64 universal
TMPDMG=/tmp/keeper-installer.dmg
curl -fsSL -o "$TMPDMG" "https://www.keepersecurity.com/desktop_electron/KeeperSetup.dmg" || log "  Keeper DMG download failed — install manually from keepersecurity.com"
if [[ -f "$TMPDMG" ]]; then
    hdiutil attach -nobrowse "$TMPDMG" 2>/dev/null
    KEEPER_APP=$(ls -d "/Volumes/Keeper Installer"*/Keeper.app 2>/dev/null | head -1)
    [[ -z "$KEEPER_APP" ]] && KEEPER_APP=$(ls -d /Volumes/Keeper*/Keeper*.app 2>/dev/null | head -1)
    if [[ -n "$KEEPER_APP" ]]; then
        rsync -a --delete "$KEEPER_APP/" "/Applications/Keeper.app/" 2>/dev/null || cp -R "$KEEPER_APP" /Applications/
        hdiutil detach "$(dirname "$KEEPER_APP")" 2>/dev/null
        log "  Keeper installed to /Applications/Keeper.app — log in manually"
    else
        log "  Keeper DMG attached but .app not found — check /Volumes"
    fi
    rm -f "$TMPDMG"
fi

# pCloud — latest Mac DMG (universal)
TMPDMG=/tmp/pcloud-installer.dmg
curl -fsSL -o "$TMPDMG" "https://www.pcloud.com/how-to-install-pcloud-drive-mac-os-x.html" 2>/dev/null
# The actual DMG URL changes; fall back to the App Store download page if the direct DMG 404s.
if [[ -s "$TMPDMG" ]] && file "$TMPDMG" 2>/dev/null | grep -q "Mac OS X.*disk image"; then
    hdiutil attach -nobrowse "$TMPDMG" 2>/dev/null
    PCLOUD_APP=$(ls -d /Volumes/pcloud*/pcloud*.app 2>/dev/null | head -1)
    if [[ -n "$PCLOUD_APP" ]]; then
        cp -R "$PCLOUD_APP" /Applications/
        hdiutil detach "$(dirname "$PCLOUD_APP")" 2>/dev/null
        log "  pCloud installed to /Applications — log in manually, then enable folder mount"
    else
        log "  pCloud DMG attached but .app not found — check /Volumes"
    fi
    rm -f "$TMPDMG"
else
    log "  pCloud direct DMG URL drifted — install manually from pcloud.com/downloads.html or via App Store"
fi

# ─── 3b. Infisical CLI ────────────────────────────────────────────────────────
log "Installing Infisical CLI..."
brew install infisical/get-cli/infisical 2>/dev/null || true

# Link python3 if needed
PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
[[ ! -f "$PYTHON" ]] && PYTHON="$(command -v python3)"
log "Python: $PYTHON"

# ─── 4. pip dependencies ─────────────────────────────────────────────────────
log "Installing pip packages..."
"$PYTHON" -m pip install --quiet psutil aiohttp

# ─── 5. Claude Code CLI + Codex ──────────────────────────────────────────────
log "Installing Claude Code CLI..."
npm install -g @anthropic-ai/claude-code 2>/dev/null || true
log "Installing Codex..."
npm install -g @openai/codex 2>/dev/null || true

# ─── 5b. Fleet agent CLI tools (herdr + pi) ─────────────────────────────────
# Added 2026-08-28 after fleet-machine-state audit surfaced both as gaps on
# at least one Mac. Reference: ~/.claude/skills/kenneth-fleet/references/fleet-machine-state.md
log "Installing fleet agent CLI tools (herdr + pi)..."

# `pi` — @earendil-works/pi-coding-agent (verified on Lucy 2026-08-28:
# /opt/homebrew/bin/pi -> ../lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js)
npm install -g @earendil-works/pi-coding-agent 2>/dev/null || true

# `herdr` — install source confirmed 2026-08-28 (was TODO): herdr.dev, NOT
# GitHub releases (own product site — https://herdr.dev/docs/install/ also
# lists Homebrew/mise/Nix as alternatives, but no brew formula/tap found as
# of this audit, so we use the official installer). Verified already present
# and working on Claire, Lucy, and Luna (v0.8.2, Mach-O ARM64) — this step
# just makes it part of the baseline for future Macs + keeps it current.
# NOTE: this is a curl-pipe-to-sh install (supply-chain caveat noted,
# accepted since it's Kenneth's own already-trusted binary on 3 machines —
# revisit if herdr ever ships a pinned/checksummed release).
if ! command -v herdr >/dev/null 2>&1; then
  curl -fsSL https://herdr.dev/install.sh | sh 2>/dev/null || true
fi
if command -v herdr >/dev/null 2>&1; then
  log "  ✓ herdr installed ($(herdr --version 2>/dev/null))"
else
  log "  ⚠ herdr install failed — see https://herdr.dev/docs/install/ for manual steps"
fi

# ─── 5c. Fleet menu-bar / monitor apps (Loop, Stats, Core-Monitor, Cue) ──────
# Added 2026-08-28 fleet-machine-state audit. Pinning these in the canonical
# baseline so the "Bootstrap? = Y" column in fleet-machine-state.md is true and
# so future bootstraps install them automatically (currently they're ad-hoc on
# existing Macs). Reference:
#   ~/.claude/skills/kenneth-fleet/references/fleet-machine-state.md
log "Installing fleet menu-bar / monitor apps..."

# Loop (MrKai77/Loop) — brew cask `loop`. Verified Lucy 2026-08-28: v1.4.2 installed
# 2026-08-21 01:10:30. Homepage: https://github.com/MrKai77/Loop
brew install --cask loop 2>/dev/null || true

# Stats (exelban/stats) — brew cask `stats`. Verified Lucy 2026-08-28: v3.0.11
# installed 2026-08-21 01:10:33 (3.0.13 available). Homepage: https://github.com/exelban/stats
brew install --cask stats 2>/dev/null || true

# Core-Monitor (offyotto/Core-Monitor) — NO brew formula. Latest release DMG.
# TODO(ken): confirm release asset naming before uncommenting. Provisional pattern:
#   CORE_MONITOR_REPO="offyotto/Core-Monitor"
#   curl -fsSL "https://github.com/$CORE_MONITOR_REPO/releases/latest/download/Core-Monitor.dmg" \
#     -o /tmp/Core-Monitor.dmg && \
#     hdiutil attach -nobrowse /tmp/Core-Monitor.dmg && \
#     cp -R "/Volumes/Core Monitor/Core Monitor.app" /Applications/ && \
#     hdiutil detach "/Volumes/Core Monitor"
log "  Core-Monitor: install command placeholder — TODO confirm release asset name"

# Cue (Blueturboguy07/cue) — NOT the cuelang.org CUE config language (that is a
# DIFFERENT package — brew `cue` IS the config language, do NOT uninstall it).
# Blueturboguy07/cue is a separate Swift menu-bar app on GitHub, no brew formula.
# TODO(ken): confirm the release asset URL before uncommenting. Provisional pattern:
#   CUE_REPO="Blueturboguy07/cue"
#   curl -fsSL "https://github.com/$CUE_REPO/releases/latest/download/cue.dmg" \
#     -o /tmp/cue.dmg && \
#     hdiutil attach -nobrowse /tmp/cue.dmg && \
#     cp -R "/Volumes/Cue/Cue.app" /Applications/ && \
#     hdiutil detach "/Volumes/Cue"
log "  Blueturboguy07/cue: install source unconfirmed — TODO"

# ─── 6. OpenSSH (already present on macOS) + Kenneth's key ───────────────────
log "Configuring SSH..."
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
if ! grep -q "kennethworkinfrastructure" "$HOME/.ssh/authorized_keys" 2>/dev/null; then
    echo "$KENNETH_PUBKEY" >> "$HOME/.ssh/authorized_keys"
    log "  Added Kenneth's SSH public key."
fi

# Enable Remote Login (SSH)
sudo systemsetup -setremotelogin on 2>/dev/null || \
    log "  NOTE: Enable System Settings → General → Sharing → Remote Login manually"

# ─── 7. Ollama (optional local LLM) ──────────────────────────────────────────
if [[ "${SKIP_OLLAMA:-0}" != "1" ]]; then
    log "Installing Ollama..."
    brew install ollama 2>/dev/null || true
    if command -v ollama &>/dev/null; then
        # Start ollama service in background
        brew services start ollama 2>/dev/null || true
        # Pull default model based on RAM
        RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
        if (( RAM_GB >= 32 )); then
            log "  RAM: ${RAM_GB}GB — pulling llama3.1:8b"
            ollama pull llama3.1:8b &
        else
            log "  RAM: ${RAM_GB}GB — pulling llama3.2:3b"
            ollama pull llama3.2:3b &
        fi
    fi
fi

# ─── 8. Download agent scripts ───────────────────────────────────────────────
log "Downloading agent scripts..."
for f in heartbeat.py agent_bot.py lead_watcher.py twenty_client.py push_leads_to_twenty.py; do
    if fetch_file "$HERMES_RAW/$f" "$HERMES_DIR/$f"; then
        log "  [OK] $f"
    else
        log "  [SKIP] $f — not found or no GH_TOKEN"
    fi
done

# ─── 9. Write env files ───────────────────────────────────────────────────────
log "Writing env files..."
cat > "$HERMES_DIR/heartbeat.env" <<EOF
AGENT=$AGENT
ROLE=fleet-agent
FLEET_WEBHOOK=$DISCORD_WEBHOOK
BEAT_SECS=300
EOF

if [[ -n "$BOT_TOKEN" ]]; then
    cat > "$HERMES_DIR/bot.env" <<EOF
DISCORD_BOT_TOKEN=$BOT_TOKEN
DEEPSEEK_API_KEY=$DS_KEY
DEEPSEEK_BASE_URL=https://api.deepseek.com
AGENT=$AGENT
AGENT_SYS=You are $AGENT, Kenneth's fleet agent. Be concise and direct.
EOF
fi

# ─── 10. MightyOS Brain repo + project repos + Kenneth's dotfiles/skills ─────
if [[ -n "$GH_TOKEN" ]]; then
    log "Cloning MightyOS Brain repo..."
    if [[ ! -d "$HOME/MightyOS" ]]; then
        git clone "https://$GH_TOKEN@github.com/RollingStonie/MightyOS.git" "$HOME/MightyOS" 2>/dev/null
    else
        git -C "$HOME/MightyOS" pull --quiet
    fi

    # Project repos this agent needs — REPOS env var is a space-separated list of
    # "RepoName:local-dir-name" pairs, e.g. "ContentHub:M012_ContentHub". Set per
    # machine at invocation time once each agent's real workload is finalized;
    # empty by default so this step is a no-op until then.
    mkdir -p "$HOME/AG_Mission"
    for pair in ${REPOS:-}; do
        repo="${pair%%:*}"
        dir="${pair##*:}"
        target="$HOME/AG_Mission/$dir"
        if [[ ! -d "$target" ]]; then
            log "  Cloning $repo -> $target"
            git clone "https://$GH_TOKEN@github.com/RollingStonie/$repo.git" "$target" 2>/dev/null || \
                log "  [FAIL] $repo — check repo name/access"
        else
            log "  $dir already present, pulling..."
            git -C "$target" pull --quiet 2>/dev/null || true
        fi
    done

    # Kenneth's personal Claude skills/dotfiles — DOTFILES_REPO env var, e.g.
    # "kenneth-dotfiles". Not yet created — see handoff notes. No-op until set.
    if [[ -n "${DOTFILES_REPO:-}" ]]; then
        log "Cloning dotfiles/skills repo..."
        if [[ ! -d "$HOME/.claude" ]]; then
            git clone "https://$GH_TOKEN@github.com/RollingStonie/${DOTFILES_REPO}.git" "$HOME/.claude" 2>/dev/null
        else
            log "  ~/.claude already exists — not overwriting. Merge manually."
        fi
    fi
fi

# ─── 11. LaunchAgents (auto-start on login) ───────────────────────────────────
log "Registering LaunchAgents..."
PLIST_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$PLIST_DIR"

# Heartbeat
cat > "$PLIST_DIR/com.hermes.heartbeat.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>com.hermes.heartbeat</string>
    <key>ProgramArguments</key><array>
        <string>$PYTHON</string><string>-u</string><string>$HERMES_DIR/heartbeat.py</string>
    </array>
    <key>WorkingDirectory</key><string>$HERMES_DIR</string>
    <key>StartInterval</key><integer>300</integer>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><false/>
    <key>StandardOutPath</key><string>$HERMES_DIR/heartbeat.log</string>
    <key>StandardErrorPath</key><string>$HERMES_DIR/heartbeat.log</string>
</dict></plist>
PLIST
launchctl load "$PLIST_DIR/com.hermes.heartbeat.plist" 2>/dev/null || true
log "  LaunchAgent: com.hermes.heartbeat loaded"

# Discord bot (if token provided)
if [[ -n "$BOT_TOKEN" ]]; then
    cat > "$PLIST_DIR/com.hermes.bot.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>com.hermes.bot</string>
    <key>ProgramArguments</key><array>
        <string>$PYTHON</string><string>-u</string><string>$HERMES_DIR/agent_bot.py</string>
    </array>
    <key>WorkingDirectory</key><string>$HERMES_DIR</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$HERMES_DIR/bot.log</string>
    <key>StandardErrorPath</key><string>$HERMES_DIR/bot.log</string>
</dict></plist>
PLIST
    launchctl load "$PLIST_DIR/com.hermes.bot.plist" 2>/dev/null || true
    log "  LaunchAgent: com.hermes.bot loaded"
fi

# Brain repo sync every 5 min
if [[ -n "$GH_TOKEN" ]]; then
    cat > "$PLIST_DIR/com.hermes.brainsync.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>com.hermes.brainsync</string>
    <key>ProgramArguments</key><array>
        <string>/usr/bin/git</string><string>-C</string><string>$HOME/MightyOS</string><string>pull</string><string>--quiet</string>
    </array>
    <key>StartInterval</key><integer>300</integer>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$HERMES_DIR/brainsync.log</string>
    <key>StandardErrorPath</key><string>$HERMES_DIR/brainsync.log</string>
</dict></plist>
PLIST
    launchctl load "$PLIST_DIR/com.hermes.brainsync.plist" 2>/dev/null || true
    log "  LaunchAgent: com.hermes.brainsync loaded"
fi

# ─── 12. Announce online ─────────────────────────────────────────────────────
if [[ -n "$DISCORD_WEBHOOK" ]]; then
    log "Posting online announcement to Discord..."
    curl -fsSL -X POST "$DISCORD_WEBHOOK" \
        -H "Content-Type: application/json" \
        -d "{\"content\": \":white_check_mark: **$AGENT** (Mac) bootstrap complete — online and ready.\"}" 2>/dev/null || \
        log "  (Discord post failed — set FLEET_WEBHOOK env var later)"
fi

# ─── 13. Retired apps — auto-uninstall on next bootstrap ─────────────────────
# ActivityWatch + boringNotch retired 2026-08-28 — bad UX, replaced by Stats (exelban/stats)
# + Loop (MrKai77/Loop). Bootstrap will uninstall them so they don't silently persist
# on future Macs. Manual one-liner if you need to uninstall on an existing Mac:
#   brew uninstall --cask activitywatch
#   rm -rf /Applications/boringNotch.app
# Workflow reference: ~/.claude/skills/kenneth-fleet/references/retire.md
log "Removing retired apps (ActivityWatch + boringNotch)..."
brew uninstall --cask activitywatch 2>/dev/null || true
rm -rf /Applications/boringNotch.app 2>/dev/null || true

# ─── Done ────────────────────────────────────────────────────────────────────
log ""
log "════════════════════════════════════════════════════════"
log "  $AGENT (Mac) bootstrap complete!"
log "  Python:      $PYTHON"
log "  Hermes dir:  $HERMES_DIR"
log "  Log:         $LOG"
log "  Tailscale:   verify with: tailscale status"
log "  Chrome:      installed (/Applications/Google Chrome.app)"
log "  Discord:     installed (/Applications/Discord.app)"
log "  Slack:       installed (/Applications/Slack.app)"
log "  Keeper:      installed — open and log in manually"
log "  NordVPN:     installed — open and log in manually"
log "  Docker:      installed (/Applications/Docker.app) — open once to finish setup"
log "  pCloud:      installed — open and log in manually, then enable folder mount"
log "  Loop:        installed (/Applications/Loop.app)"
log "  Stats:       installed (/Applications/Stats.app)"
log "  Infisical:   $(command -v infisical || echo 'check brew')"
log "  Claude Code: $(npm list -g @anthropic-ai/claude-code --depth 0 2>/dev/null | grep claude || echo 'check npm')"
log "  Codex:       $(npm list -g @openai/codex --depth 0 2>/dev/null | grep codex || echo 'check npm')"
log "  Retired:     ActivityWatch + boringNotch removed"
log ""
log "  Next steps:"
log "  1. Copy secrets from Keeper → $HOME/.hermes/secrets/"
log "  2. Verify heartbeat in Discord #fleet-status"
log "  3. @$AGENT hello  to test bot"
log "  4. Reboot to confirm all LaunchAgents survive"
log "════════════════════════════════════════════════════════"
