#!/usr/bin/env bash
# Wrapper for movie-audit.py: resolves the ts-radarr container IP and reads
# the Radarr API key from config.xml under CONFIG_ROOT, then runs the audit.
# All arguments are passed through (e.g. --issue banned-group, --json).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CONFIG_ROOT="$(grep '^CONFIG_ROOT=' "$REPO_ROOT/.env" | cut -d= -f2)"
if [[ -z "$CONFIG_ROOT" ]]; then
    echo "error: CONFIG_ROOT not found in $REPO_ROOT/.env" >&2
    exit 1
fi

RADARR_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ts-radarr 2>/dev/null)"
if [[ -z "$RADARR_IP" ]]; then
    echo "error: could not resolve ts-radarr container IP — is the stack running?" >&2
    exit 1
fi

RADARR_API_KEY="$(sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' "$CONFIG_ROOT/Radarr/Config/config.xml")"
if [[ -z "$RADARR_API_KEY" ]]; then
    echo "error: could not read ApiKey from $CONFIG_ROOT/Radarr/Config/config.xml" >&2
    exit 1
fi

RADARR_URL="http://$RADARR_IP:7878" RADARR_API_KEY="$RADARR_API_KEY" \
    exec python3 "$REPO_ROOT/scripts/movie-audit.py" "$@"
