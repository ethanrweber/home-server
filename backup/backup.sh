#!/usr/bin/env bash
# Nightly backup of the stack with restic. Run as root by restic-backup.service, which loads
# the repository settings from /etc/restic/b2.env, and starts the stack again if this fails.
# Run it by hand: sudo systemctl start restic-backup     Logs: journalctl -u restic-backup
set -euo pipefail
cd "$(dirname "$0")/.."   # the stack's checkout

# where appdata and media live, from the stack's .env. read, not sourced: some values in it
# aren't safe to run as shell
CONFIG_ROOT=$(grep -m1 '^CONFIG_ROOT=' .env | cut -d= -f2-)
MEDIA_ROOT=$(grep -m1 '^MEDIA_ROOT=' .env | cut -d= -f2-)
export CONFIG_ROOT MEDIA_ROOT   # excludes.txt uses them

restic unlock   # remove a lock left behind by an interrupted run

# stop the stack while it's backed up, so no database changes halfway through being read
docker compose stop
restic backup --exclude-file=backup/excludes.txt \
    "$PWD" "$CONFIG_ROOT" /etc/restic "$MEDIA_ROOT/books" "$MEDIA_ROOT/music"
docker compose start

restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 12

# sundays: free the space of forgotten snapshots, and re-read a tenth of the backup to check it
if [ "$(date +%u)" = 7 ]; then
    restic prune
    restic check --read-data-subset=10%
fi

# tell healthchecks.io it worked. if anything above failed, this never runs, and it alerts you
curl -fsS -m 10 --retry 3 -o /dev/null "$HC_URL"
