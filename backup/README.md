# backups

encrypted, deduplicated backups of the stack with [restic](https://restic.net): its secrets, every app's config and database, and the small media libraries. what to do when something breaks is in [RECOVERY.md](../RECOVERY.md).

## how it works

a nightly systemd timer runs `restic-backup backup <repo>` as root. each run, for one repository:

1. takes a lock, so two runs never overlap, and clears stale restic locks left by an interrupted run
2. runs the coverage checks (below)
3. copies every SQLite database it finds with SQLite's backup API, checks each copy, and excludes the live file from the snapshot. a live database copied mid-write can be corrupt; the copy can't be. if a copy fails, the live file is backed up as-is and the run fails
4. backs up each set as its own tagged snapshot, then `restic forget`s that set's old snapshots
5. opens each new snapshot and checks that the database copies and the canary files (`.env`, a sidecar's `tailscaled.state`) are really in it
6. pings healthchecks.io: success, or failure with the problems in the ping body

a problem never stops a run: everything that can be backed up still is, and the run is marked failed. a problem on this machine (a failed copy, a coverage check, an unreadable file) is reported to every repository's healthcheck; a problem reaching a repository only to that repository's.

a daily `restic-backup maint <repo>` prunes and checks when due: weekly `prune` and `check`, and each month `check --read-data-subset=<month>/12`, which re-reads a different twelfth of the data so all of it is read once a year.

## sets

a set is a list of paths backed up together, with its own retention. [sets/sets.conf](sets/sets.conf) defines them; which sets a repository receives is `SETS=` in `/etc/restic/<repo>.env`.

| set | contents | kept |
|---|---|---|
| `stack` | this checkout including the gitignored `.env` files, all of `${CONFIG_ROOT}` minus [sets/stack.exclude](sets/stack.exclude), `/etc/restic`, the database copies | 7 daily, 4 weekly, 12 monthly |
| `appzips` | the backups the apps write themselves ([sets/appzips.paths](sets/appzips.paths)) | the latest only: zips don't deduplicate |
| `media` | books (with calibre's `metadata.db`, copied like any database) and music | as stack |
| `comics` | comics | as stack |

each file belongs to one set: a path listed by one set is excluded from any other set containing it. set lists use `${REPO_ROOT}`, `${CONFIG_ROOT}`, `${MEDIA_ROOT}` and `${DUMPS}`, filled in from `/etc/restic/backup.conf` and this checkout's `.env` (which is read, never sourced). every exclude must start with a path, because restic applies a pattern without one at any depth.

paths for one machine only go in `/etc/restic/<set>.local.paths`, same format, not in git.

## coverage checks

these fail the run when data would otherwise be missed without any error:

- a path in a set list doesn't exist
- a container keeps data in a Docker volume that isn't in [volumes.allow](volumes.allow). volumes live outside every set
- a Postgres or MariaDB data directory turns up in a set. a copy of a running database server isn't safe to restore; it needs its own dump step
- a `.zip`/`.tar.gz` from the last 60 days sits under `${CONFIG_ROOT}` outside the app backup folders: probably an app's own backups, which belong in `appzips`
- a database found last run has disappeared (reported once)
- restic couldn't read a file (it exits 3) that isn't in [read-errors.allow](read-errors.allow)

## files

```
restic-backup         the runner: backup, maint, show, restic (a pass-through with the repo's settings)
restore-db            put database copies back in place
restore-test          restore into a scratch directory with only the Bitwarden values, and compare
lib/resticbackup/     the runner's code
sets/                 the set lists
volumes.allow, read-errors.allow
systemd/              services and timers
etc/b2.env.example    template for /etc/restic/b2.env
install.sh            installs all of the above
tests/                python3 -m unittest, run from tests/ (fake restic and docker; no root)
```

`install.sh` copies everything to `/usr/local/lib/restic-backup` and the units to `/etc/systemd/system`, and root only ever runs those copies: switching branches in this checkout can't change what a backup does. after changing anything here, re-run `sudo bash backup/install.sh`; it shows a diff and asks first.

on the machine:

```
/etc/restic/backup.conf         REPO_ROOT (this checkout) and BACKUP_HOST (the --host of every snapshot)
/etc/restic/<repo>.env, .pass   repository settings and password (root only)
/var/lib/restic-dumps/          this run's database copies
/var/lib/restic-backup/         what was found last run, maintenance stamps
/var/lib/restic-cache/          restic's cache
```

## setting up

1. install restic 0.19.1 or newer, verifying the release signature:
   ```
   sudo apt install bzip2 gnupg
   cd "$(mktemp -d)"
   v=0.19.1
   base=https://github.com/restic/restic/releases/download/v$v
   curl -fLO "$base/restic_${v}_linux_amd64.bz2" -fLO "$base/SHA256SUMS" -fLO "$base/SHA256SUMS.asc"
   curl -fsSL https://restic.net/gpg-key-alex.asc | gpg --import
   gpg --verify SHA256SUMS.asc SHA256SUMS      # Good signature, key CF8F 18F2 8445 7597 3F79 D4E1 91A6 868B D3F7 A907
   sha256sum --ignore-missing -c SHA256SUMS    # restic_0.19.1_linux_amd64.bz2: OK
   bunzip2 "restic_${v}_linux_amd64.bz2"
   sudo install -m 755 "restic_${v}_linux_amd64" /usr/local/bin/restic
   restic version
   ```
   later updates: `sudo restic self-update` (it verifies the signature itself).
2. `sudo bash backup/install.sh` from the stack's checkout (the one with the `.env`).
3. create the repository's settings from the template and Bitwarden:
   ```
   sudo install -m 600 backup/etc/b2.env.example /etc/restic/b2.env
   sudo install -m 600 /dev/null /etc/restic/b2.pass
   sudoedit /etc/restic/b2.env /etc/restic/b2.pass
   ```
4. `sudo restic-backup restic b2 init` (once, to create the repository)
5. `sudo restic-backup show b2` to see what each set covers, then `sudo restic-backup backup b2 --dry-run`
6. the first backup, by hand: `sudo restic-backup backup b2`
7. once through systemd, which runs with a different environment: `sudo systemctl start restic-backup@b2`, then `journalctl -u restic-backup@b2`
8. `sudo restore-test` (see [RECOVERY.md](../RECOVERY.md#testing-a-restore))
9. create two healthchecks.io checks (backup and maintenance, each expecting a ping a day), put their ping URLs in `/etc/restic/b2.env`, then turn on the schedule: `sudo systemctl enable --now restic-backup-nightly@b2.timer restic-maint@b2.timer`

## everyday

```
journalctl -u restic-backup@b2                    # last runs
sudo restic-backup restic b2 snapshots            # any restic command, with the repository's settings
sudo restic-backup show b2                        # what each set backs up
sudo restic-backup backup b2                      # a backup now
```

## changing what's backed up

- **a new app under `${CONFIG_ROOT}`**: nothing to do. its files are in `stack` and its SQLite databases are found and copied automatically
- **an app keeping data elsewhere** (a photo library, say): add the path to a set, or a new set in `sets.conf` plus its `.paths`, and the set to `SETS=` of each repository that should get it. mind the 10 GB of the b2 free tier
- **an app with its own backup folder**: add the folder to `sets/appzips.paths`
- **a Postgres or MariaDB container**: needs a dump step before it can be backed up safely
- **something disposable**: add it to `sets/stack.exclude`
- then `sudo bash backup/install.sh` and `sudo restic-backup show b2`
