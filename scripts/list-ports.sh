#!/usr/bin/env bash
# Scans all service yml files and prints a port reference table.

set -euo pipefail
cd "$(dirname "$0")/.."

printf "%-8s %-15s %s\n" "HOST" "CONTAINER" "SERVICE"
printf "%-8s %-15s %s\n" "----" "---------" "-------"

grep -rn '^\s*- .*[0-9]:[0-9]' services/ --include="*.yml" | grep -v '/dev/' | while IFS= read -r line; do
  file=$(echo "$line" | cut -d: -f1 | sed 's|services/||;s|\.yml||;s|.*/||')
  content=$(echo "$line" | cut -d: -f3-)
  host_port=$(echo "$content" | sed 's/[" -]//g' | cut -d: -f1)
  rest=$(echo "$content" | sed 's/[" -]//g' | cut -d: -f2)
  container_port=$(echo "$rest" | sed 's/#.*//' | tr -d ' ')
  comment=$(echo "$content" | grep -o '#.*' 2>/dev/null | sed 's/^# *//' || true)
  if [ -z "$comment" ]; then
    service="$file"
  elif echo "$comment" | grep -qi "$file\|sonarr\|radarr\|calibre\|qbittorrent\|shadowsocks"; then
    service="$comment"
  else
    service="$file ($comment)"
  fi
  printf "%-8s %-15s %s\n" "$host_port" "$container_port" "$service"
done | sort -n
