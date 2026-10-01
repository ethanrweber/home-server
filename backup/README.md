# backups

every night at 04:30, `restic-backup.timer` runs [backup.sh](backup.sh), which:

1. stops the stack, so no database changes while it's being read
2. backs up, with [restic](https://restic.net), this checkout (including the gitignored `.env` files), all of `$CONFIG_ROOT` except [excludes.txt](excludes.txt), `/etc/restic`, and the books and music libraries
3. starts the stack again. it's down for a few minutes
4. forgets snapshots beyond 7 daily, 4 weekly and 12 monthly
5. on sundays, prunes what forgotten snapshots used and re-reads a random 10% of the backup to check it
6. pings healthchecks.io

if any step fails, the service starts the stack anyway, and the ping never arrives, so healthchecks.io emails you. why it failed is in `journalctl -u restic-backup`.

the repository's settings and keys live only in `/etc/restic/b2.env` and `/etc/restic/b2.pass` (root only). their source of truth is Bitwarden.

## setting up

run these from the stack's checkout.

1. install restic, verifying the release's signature:
   ```
   sudo apt install bzip2
   cd "$(mktemp -d)"
   v=0.19.1
   base=https://github.com/restic/restic/releases/download/v$v
   curl -fLO "$base/restic_${v}_linux_amd64.bz2" -fLO "$base/SHA256SUMS" -fLO "$base/SHA256SUMS.asc"
   curl -fsSL https://restic.net/gpg-key-alex.asc | gpg --import
   gpg --verify SHA256SUMS.asc SHA256SUMS      # Good signature, key CF8F 18F2 8445 7597 3F79 D4E1 91A6 868B D3F7 A907
   sha256sum --ignore-missing -c SHA256SUMS    # restic_0.19.1_linux_amd64.bz2: OK
   bunzip2 "restic_${v}_linux_amd64.bz2"
   sudo install -m 755 "restic_${v}_linux_amd64" /usr/local/bin/restic
   cd -   # back to the checkout
   ```
   to update later: `sudo restic self-update`
2. fill in the settings from Bitwarden:
   ```
   sudo install -d -m 700 /etc/restic
   sudo install -m 600 backup/b2.env.example /etc/restic/b2.env
   sudo install -m 600 /dev/null /etc/restic/b2.pass
   sudoedit /etc/restic/b2.env /etc/restic/b2.pass
   ```
3. create the repository (once): `sudo -i`, then `set -a; . /etc/restic/b2.env; set +a` and `restic init`
4. on healthchecks.io, add a check with a period of 1 day and a grace time of 2 hours, and put its ping URL in `b2.env` as `HC_URL`
5. install the units, and run the first backup by hand. it uploads a few GB, and the stack is down until it's done:
   ```
   sudo systemctl link "$PWD/backup/restic-backup.service" "$PWD/backup/restic-backup.timer"
   sudo systemctl start restic-backup
   journalctl -u restic-backup -f
   ```
6. turn on the schedule: `sudo systemctl enable --now restic-backup.timer`

## using the backup

restic needs the repository's settings loaded first, as root:

```
sudo -i
set -a; . /etc/restic/b2.env; set +a
restic snapshots                                      # what's there
mkdir -p /mnt/restic; restic mount /mnt/restic       # browse every snapshot as folders (ctrl-c to unmount)
restic restore latest --target /tmp/restore --include /path/to/what/you/want
```

restored files keep their owner and permissions. to roll back an app, stop it (`docker compose stop sonarr`), restore its folder under `$CONFIG_ROOT`, copy it over the live one, and start it again. the stack was stopped while the snapshot was taken, so its database files are consistent as they are.

## changing what's backed up

- paths: the `restic backup` line in [backup.sh](backup.sh)
- things to leave out: [excludes.txt](excludes.txt)
- retention: the `restic forget` line
- when it runs: [restic-backup.timer](restic-backup.timer), then `sudo systemctl daemon-reload`
