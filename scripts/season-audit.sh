#!/usr/bin/env bash
# Wrapper for season-audit.py: resolves the ts-sonarr container IP and reads
# the Sonarr API key from config.xml under CONFIG_ROOT, then runs the audit.
# All arguments are passed through (e.g. --min-score 20, --json).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CONFIG_ROOT="$(grep '^CONFIG_ROOT=' "$REPO_ROOT/.env" | cut -d= -f2)"
if [[ -z "$CONFIG_ROOT" ]]; then
    echo "error: CONFIG_ROOT not found in $REPO_ROOT/.env" >&2
    exit 1
fi

SONARR_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ts-sonarr 2>/dev/null)"
if [[ -z "$SONARR_IP" ]]; then
    echo "error: could not resolve ts-sonarr container IP — is the stack running?" >&2
    exit 1
fi

SONARR_API_KEY="$(sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' "$CONFIG_ROOT/Sonarr/Config/config.xml")"
if [[ -z "$SONARR_API_KEY" ]]; then
    echo "error: could not read ApiKey from $CONFIG_ROOT/Sonarr/Config/config.xml" >&2
    exit 1
fi

SONARR_URL="http://$SONARR_IP:8989" SONARR_API_KEY="$SONARR_API_KEY" \
    exec python3 "$REPO_ROOT/scripts/season-audit.py" "$@"
