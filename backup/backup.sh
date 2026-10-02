#!/usr/bin/env bash
# nightly restic backup of the stack. restic-backup.service runs this script as root, with the
# repository settings from /etc/restic/b2.env. if the script fails, the service starts the stack.
# to run a backup manually: sudo systemctl start restic-backup
# to read the log: journalctl -u restic-backup
set -euo pipefail
cd "$(dirname "$0")/.."   # the stack checkout

# read only these two values from the stack's .env. do not source it: some of its values are not
# shell-safe
CONFIG_ROOT=$(grep -m1 '^CONFIG_ROOT=' .env | cut -d= -f2-)
MEDIA_ROOT=$(grep -m1 '^MEDIA_ROOT=' .env | cut -d= -f2-)
export CONFIG_ROOT MEDIA_ROOT   # excludes.txt uses these values

# the b2 usage widget on homepage reads this file. homepage serves only the files that exist when
# it starts, so create the file before the stack restarts
STATS_FILE=$CONFIG_ROOT/Homepage/backup/b2-stats.json
mkdir -p "$(dirname "$STATS_FILE")"
touch "$STATS_FILE"

restic unlock   # remove stale locks that an interrupted run left behind

# stop the stack during the backup, so that the databases stay consistent while restic reads them
docker compose stop
restic backup --exclude-file=backup/excludes.txt \
    "$PWD" "$CONFIG_ROOT" /etc/restic "$MEDIA_ROOT/books" "$MEDIA_ROOT/music"
docker compose start

restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 12

# on sundays, prune the data that no snapshot references, then verify a random 10% of the pack data
if [ "$(date +%u)" = 7 ]; then
    restic prune
    restic check --read-data-subset=10%
fi

# write the size of the repository for the homepage widget, after forget and prune
restic stats --mode raw-data --json > "$STATS_FILE"

# ping healthchecks.io. set -e stops the script at the first failed command, so a failure skips
# this ping and healthchecks.io alerts you
curl -fsS -m 10 --retry 3 -o /dev/null "$HC_URL"
