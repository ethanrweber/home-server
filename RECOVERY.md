# recovery

how to get things back from the backups described in [backup/README.md](backup/README.md). no secret is written here: each step names the Bitwarden entry to take it from.

| Bitwarden entry | holds |
|---|---|
| **Backblaze account** | the Backblaze login, and its 2FA backup codes |
| **B2 app key — restic** | keyID and applicationKey; the bucket name and endpoint are in its notes |
| **restic repo password — b2** | the repository password. without it the backups can't be read, by anyone |

the Bitwarden master password and 2FA recovery code are on paper, not on any computer.

all commands run as root. `restic-backup restic b2 <command>` runs any restic command with the repository's settings; on a machine without the backup installed yet, see [the machine is gone](#the-machine-is-gone).

## one app is broken

roll its database back to last night's copy:

```
sudo restic-backup restic b2 snapshots --tag stack      # to pick an older snapshot than the latest
sudo restore-db sonarr                                  # or: sudo restore-db --snapshot <id> sonarr
```

restore-db shows what it will do and asks first. it checks the copy, stops the containers using that folder, moves the current database and its `-wal`/`-shm` aside as `*.pre-restore-<time>` (to undo: stop the app, move them back), puts the copy in with its original owner and mode, and starts the containers again.

for other files of the app, restore into a scratch directory and copy what you need, with the app stopped:

```
sudo restic-backup restic b2 restore latest --tag stack --target /tmp/restore \
    --include "$CONFIG_ROOT/Sonarr/Config/config.xml"
```

(`$CONFIG_ROOT` is the value in the stack's `.env`.) to look around first, mount the repository read-only in one terminal and browse it as root (`sudo -i`) from another; ctrl-c unmounts:

```
sudo mkdir -p /mnt/restic && sudo restic-backup restic b2 mount /mnt/restic
```

## books or music are gone

```
sudo restic-backup restic b2 restore latest --tag media --target /tmp/restore \
    --include "$MEDIA_ROOT/books/Some Author"
```

then copy them back into place. restoring the whole library: stop calibre-web-automated, restore `$MEDIA_ROOT/books` the same way and move it into place, then put its database back with `sudo restore-db --set media --all`.

comics aren't in b2 (they don't fit the free tier).

## the machine is gone

the host and its media storage survived; the machine running the stack didn't. rebuild on a new one.

a whole-stack restore downloads a gigabyte or more. with no card on the Backblaze account, downloads beyond the free daily allowance fail ("cap exceeded"); if that happens, add a card and a small daily download cap as in [b2 is full](#b2-is-full), or wait a day and run the same command again.

1. set up a Linux machine with Docker, and mount the media storage where it was before.
2. clone the repo to the same path as before (`REPO_ROOT`; `restic snapshots` lists the paths if you've forgotten):
   ```
   git clone https://github.com/ethanrweber/home-server.git <REPO_ROOT>
   ```
3. install restic, as in [backup/README.md](backup/README.md#setting-up) step 1.
4. write the repository's settings from Bitwarden:
   ```
   sudo install -d -m 700 /etc/restic
   sudo install -m 600 <REPO_ROOT>/backup/etc/b2.env.example /etc/restic/b2.env
   sudo install -m 600 /dev/null /etc/restic/b2.pass
   sudoedit /etc/restic/b2.env /etc/restic/b2.pass
   ```
5. restore the stack's latest snapshot in place. that brings back both `.env` files, every app's config, every sidecar's tailscale identity, and `/etc/restic`. the database copies come in the next step, so they're left out here:
   ```
   sudo -i
   set -a; . /etc/restic/b2.env; set +a
   restic snapshots --tag stack
   restic restore latest --tag stack --target / --exclude /var/lib/restic-dumps
   exit
   ```
6. install the backup, and put every database back. do this **before** the first `docker compose up`, or the apps start with new, empty databases:
   ```
   sudo bash <REPO_ROOT>/backup/install.sh
   sudo restore-db --set stack --all
   ```
7. the machine's own tailscale node (not the sidecars): `sudo tailscale up`, then in the [admin console](https://login.tailscale.com/admin/machines) remove the old machine, give the new one its name, and disable its key expiry.
8. start the stack: `docker compose up -d` in `<REPO_ROOT>`. the sidecars rejoin as themselves from their restored state. one whose state was lost needs a fresh `TS_AUTHKEY` in `.env` first (auth keys last 90 days at most, so the restored one is probably dead).
9. turn the schedule back on: `sudo systemctl enable --now restic-backup-nightly@b2.timer restic-maint@b2.timer`.

never run the old and the new machine's stack at the same time: the sidecars would fight over their tailscale identities.

## b2 is full

there's no card on the Backblaze account, so past the 10 GB free tier uploads fail ("storage cap exceeded") and nothing is billed. restic's cleanup needs a little room too, so a full repository can't clean itself up. to get out:

1. sign in as **Backblaze account**, add a card, and set a small daily storage cap under **Caps & Alerts** (caps are unlimited once a card is on file, so set one straight away).
2. make room by keeping fewer snapshots, for now: `sudo restic-backup restic b2 forget --group-by host,tags --keep-daily 3 --keep-monthly 3 --prune`
3. so it doesn't fill up again, shorten retention in `backup/sets/sets.conf` or move something out of b2's sets, then `sudo bash backup/install.sh` and `sudo restic-backup backup b2`.
4. once it's back well under 10 GB, remove the card, or set every cap to $0.

## Bitwarden is lost

the paper has the master password and the 2FA recovery code. if the repository password is gone as well, the b2 backups can't be read; start a new repository with a new password, and store it in Bitwarden.

## testing a restore

once when the backup is new, then yearly: prove the backups can rebuild the machine using nothing but Bitwarden.

```
sudo restore-test
```

it asks for the endpoint, bucket, keyID, applicationKey and repository password (from Bitwarden; of `/etc/restic` it reads only `backup.conf`, for the paths to compare), restores the latest stack snapshot into a scratch directory with an empty cache, and checks it against the live machine: both `.env` files byte-identical, every sidecar's state present, every database copy intact with its original owner and mode, Tautulli's play history complete, and `/etc/restic` restored. the scratch directory is deleted afterwards, since it holds every secret. it downloads the whole stack snapshot, so it is also the test of whether a full restore fits in b2's free download allowance: if it stops with "cap exceeded", note that down here, and see [the machine is gone](#the-machine-is-gone).

a full rebuild drill, on a scratch machine, must never join the tailnet with the restored sidecar state, mount the media storage read-write, or start gluetun, qbittorrent or the arrs' download clients.
