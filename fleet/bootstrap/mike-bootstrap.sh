#!/usr/bin/env bash
# mike-bootstrap.sh — run on a fresh Windows/WSL Dell to restore Mike's agent
# Prereq: Tailscale installed and joined to the Hermes network

set -e
echo "=== Mike bootstrap starting ==="

# 1. Install Ollama and pull model
if ! command -v ollama &>/dev/null; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
ollama pull llama3.2:3b

# 2. Clone or update Hermes repo
HERMES_DIR="$HOME/hermes"
if [ ! -d "$HERMES_DIR" ]; then
  GITHUB_TOKEN=$(cat ~/.hermes/secrets/github_token.json | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
  git clone "https://$GITHUB_TOKEN@github.com/RollingStonie/Hermes.git" "$HERMES_DIR"
else
  git -C "$HERMES_DIR" pull
fi

# 3. Clone or update Brain repo
BRAIN_DIR="$HOME/MightyOS"
if [ ! -d "$BRAIN_DIR" ]; then
  GITHUB_TOKEN=$(cat ~/.hermes/secrets/github_token.json | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
  git clone "https://$GITHUB_TOKEN@github.com/RollingStonie/MightyOS.git" "$BRAIN_DIR"
else
  git -C "$BRAIN_DIR" pull
fi

# 4. Install Python deps
pip3 install psutil requests 2>/dev/null || true

# 5. Restore secrets (Keeper → ~/.hermes/secrets/ must be done manually first)
echo "ACTION REQUIRED: Copy secrets from Keeper to ~/.hermes/secrets/"
echo "  Needed: github_token.json, twenty_api_key.json, discord_webhooks.json"

# 6. Start heartbeat
export AGENT=Mike
export ROLE="lead-gen / google-maps-scraping"
export BRAIN_REPO_PATH="$BRAIN_DIR"
nohup python3 "$HERMES_DIR/sync/heartbeat.py" --loop > ~/.hermes/heartbeat.log 2>&1 &
echo "Heartbeat started (PID $!)"

echo "=== Mike bootstrap complete. Check Plane for active ticket. ==="
