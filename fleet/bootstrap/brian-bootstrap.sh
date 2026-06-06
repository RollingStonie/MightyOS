#!/usr/bin/env bash
# brian-bootstrap.sh — restore Brian's agent (24/7 sender)
set -e
echo "=== Brian bootstrap starting ==="

BRAIN_DIR="$HOME/MightyOS"
if [ ! -d "$BRAIN_DIR" ]; then
  GITHUB_TOKEN=$(cat ~/.hermes/secrets/github_token.json | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
  git clone "https://$GITHUB_TOKEN@github.com/RollingStonie/MightyOS.git" "$BRAIN_DIR"
fi

pip3 install psutil requests 2>/dev/null || true

echo "ACTION REQUIRED: Copy secrets from Keeper to ~/.hermes/secrets/"
echo "ACTION REQUIRED: Configure outreach sending credentials"
echo "=== Brian bootstrap complete ==="
