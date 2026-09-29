#!/usr/bin/env bash
# Install (or update) the backup runner, its set lists and its systemd units from this
# checkout. Run with sudo. It shows what would change and asks first.
#
# It never writes repository settings or passwords (/etc/restic/<repo>.env, <repo>.pass),
# never enables a timer and never touches a container. Root runs only the installed
# copies, so switching branches in this checkout can't change what a backup does.
set -euo pipefail

min_restic=0.19.1
lib=/usr/local/lib/restic-backup
units=/etc/systemd/system
src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$src/.." && pwd)"

die() { echo "install.sh: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
command -v restic >/dev/null || die "restic is not installed (see backup/README.md)"
have="$(restic version | awk '{print $2}')"
[[ "$(printf '%s\n%s\n' "$min_restic" "$have" | sort -V | head -1)" == "$min_restic" ]] \
    || die "restic $have is older than $min_restic"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "python3 3.10 or newer is required"
command -v docker >/dev/null || die "docker is not installed"
[[ -f "$repo_root/.env" ]] || die "$repo_root/.env is missing"

# stage the new copy, then show how it differs from what is installed
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/lib" "$stage/units"
cp -r "$src/restic-backup" "$src/restore-db" "$src/restore-test" "$src/lib" "$src/sets" \
    "$src/volumes.allow" "$src/read-errors.allow" "$stage/lib/"
find "$stage/lib" -name __pycache__ -prune -exec rm -rf {} +
cp "$src"/systemd/*.service "$src"/systemd/*.timer "$stage/units/"

changes=0
if [[ -d $lib ]]; then
    diff -ruN "$lib" "$stage/lib" --exclude=__pycache__ || changes=1
else
    echo "new install: $lib"; changes=1
fi
for unit in "$stage"/units/*; do
    diff -uN "$units/$(basename "$unit")" "$unit" || changes=1
done
if [[ ! -f /etc/restic/backup.conf ]]; then
    echo "new: /etc/restic/backup.conf (REPO_ROOT=$repo_root BACKUP_HOST=$(hostname -s))"
    changes=1
fi
if [[ $changes -eq 0 ]]; then
    echo "already up to date"
    exit 0
fi

read -r -p "install these changes? [y/N] " answer
[[ $answer == [yY]* ]] || { echo "nothing changed"; exit 1; }

rm -rf "$lib.new"
cp -r "$stage/lib" "$lib.new"
chown -R root:root "$lib.new"
chmod -R go-w "$lib.new"
rm -rf "$lib.old"
[[ -d $lib ]] && mv "$lib" "$lib.old"
mv "$lib.new" "$lib"
rm -rf "$lib.old"
ln -sf "$lib/restic-backup" /usr/local/sbin/restic-backup
ln -sf "$lib/restore-db" /usr/local/sbin/restore-db
ln -sf "$lib/restore-test" /usr/local/sbin/restore-test

install -m 644 -o root -g root "$stage"/units/* "$units/"
systemctl daemon-reload

install -d -m 700 /etc/restic /var/lib/restic-dumps /var/lib/restic-cache /var/lib/restic-backup
if [[ ! -f /etc/restic/backup.conf ]]; then
    # BACKUP_HOST is what every snapshot is recorded under; keep it the same on a rebuilt machine
    printf 'REPO_ROOT=%s\nBACKUP_HOST=%s\n' "$repo_root" "$(hostname -s)" > /etc/restic/backup.conf
    chmod 600 /etc/restic/backup.conf
fi

echo "installed. check what each set covers with: sudo restic-backup show <repo>"
