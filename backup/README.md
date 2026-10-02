# backups

every night at 04:30, `restic-backup.timer` runs [backup.sh](backup.sh). the script:

1. stops the stack, so that the databases stay consistent while restic reads them.
2. backs up this checkout (with its gitignored `.env` files), `$CONFIG_ROOT`, `/etc/restic`, and the books and music libraries to the b2 repository with [restic](https://restic.net).
3. starts the stack again. the stack is down for a few minutes.
4. applies the retention policy: it forgets all snapshots except the last 7 daily, 4 weekly, and 12 monthly ones.
5. on sundays, prunes the data that no snapshot references, then verifies a random 10% of the pack data.
6. writes the size of the repository to `$CONFIG_ROOT/Homepage/backup/b2-stats.json`. the "Restic Backup" tile on homepage shows it as a percentage of the 10 GB b2 free tier.
7. pings healthchecks.io.

[excludes.txt](excludes.txt) lists the paths that the backup excludes.

if a step fails, the script stops and the service starts the stack again. healthchecks.io does not receive the ping, so it alerts you by email. the log is in `journalctl -u restic-backup`.

the repository settings and keys are only in `/etc/restic/b2.env` and `/etc/restic/b2.pass`, which only root can read. bitwarden is their source of truth.

## installation

run these steps from the stack checkout.

1. install restic. the commands verify the release's checksum and its gpg signature:
   ```
   sudo apt install bzip2
   cd "$(mktemp -d)"
   v=0.19.1
   base=https://github.com/restic/restic/releases/download/v$v
   curl -fLO "$base/restic_${v}_linux_amd64.bz2" -fLO "$base/SHA256SUMS" -fLO "$base/SHA256SUMS.asc"
   curl -fsSL https://restic.net/gpg-key-alex.asc | gpg --import
   gpg --verify SHA256SUMS.asc SHA256SUMS      # must show "Good signature" and key CF8F 18F2 8445 7597 3F79 D4E1 91A6 868B D3F7 A907
   sha256sum --ignore-missing -c SHA256SUMS    # must show "restic_0.19.1_linux_amd64.bz2: OK"
   bunzip2 "restic_${v}_linux_amd64.bz2"
   sudo install -m 755 "restic_${v}_linux_amd64" /usr/local/bin/restic
   cd -                                        # back to the stack checkout
   ```
   to update restic later, run `sudo restic self-update`.
2. create the root-only settings files. then fill them in from bitwarden:
   ```
   sudo install -d -m 700 /etc/restic
   sudo install -m 600 backup/b2.env.example /etc/restic/b2.env
   sudo install -m 600 /dev/null /etc/restic/b2.pass
   sudoedit /etc/restic/b2.env /etc/restic/b2.pass
   ```
3. initialize the repository. do this only once:
   ```
   sudo -i
   set -a; . /etc/restic/b2.env; set +a
   restic init
   ```
4. on healthchecks.io, add a check with a period of 1 day and a grace time of 2 hours. set its ping url as `HC_URL` in `b2.env`.
5. link the units into systemd:
   ```
   sudo systemctl link "$PWD/backup/restic-backup.service" "$PWD/backup/restic-backup.timer"
   ```
   systemd runs the units from this checkout. keep the checkout at `STACK_DIR`, on a branch that contains `backup/`.
6. run the first backup manually. it uploads a few GB, and the stack stays down until it finishes:
   ```
   sudo systemctl start --no-block restic-backup
   journalctl -u restic-backup -f              # follow the log. ctrl+c stops it
   ```
7. enable the timer:
   ```
   sudo systemctl enable --now restic-backup.timer
   ```

## restore

restic reads its settings from the environment. before you use it, open a root shell and load them:

```
sudo -i
set -a; . /etc/restic/b2.env; set +a
```

then use these commands:

```
restic snapshots                                  # list the snapshots
mkdir -p /mnt/restic; restic mount /mnt/restic    # browse every snapshot as folders. ctrl+c unmounts
restic restore latest --target /tmp/restore --include /path/to/restore
```

restored files can contain secrets. delete `/tmp/restore` when you are done.

to roll an app back to a snapshot:

1. stop the app. for example: `docker compose stop sonarr`
2. restore the app's folder under `$CONFIG_ROOT` to `/tmp/restore`. the value of `$CONFIG_ROOT` is in the stack's `.env`.
3. replace the live folder with the restored copy.
4. start the app.

restic restores file ownership and permissions. the stack was down during the backup, so the database files in each snapshot are consistent and usable as-is.

to rebuild the stack on a new machine:

1. install docker and restic. for restic, follow [installation](#installation) step 1.
2. mount the media storage at the same path as before.
3. clone this repository to the same path as before.
4. create the settings files: installation step 2. leave `HC_URL` empty. the restore in step 6 replaces the file with the original, which includes `HC_URL`.
5. open a root shell and load the settings, as above.
6. restore the latest snapshot in place. this brings back the checkout with its `.env` files, all appdata, and `/etc/restic`:
   ```
   restic restore latest --target /
   ```
   if the media storage survived, add `--exclude` for the books and music folders.
7. link the units and enable the timer: installation steps 5 and 7.
8. make sure that the old machine does not run the stack. the sidecars on both machines use the same tailscale identities.
9. start the stack: `docker compose up -d`. the sidecars rejoin the tailnet with their restored state. a sidecar without state needs a new `TS_AUTHKEY` in `.env`.

## change the backup

- to change the paths, edit the `restic backup` line in [backup.sh](backup.sh).
- to exclude a path, add it to [excludes.txt](excludes.txt).
- to change the retention policy, edit the `restic forget` line in [backup.sh](backup.sh).
- to change the schedule, edit [restic-backup.timer](restic-backup.timer). then run `sudo systemctl daemon-reload`.
