#!/usr/bin/env python3
"""Point the poster and background paths in Tautulli history at current keys.

Tautulli stores a snapshot of each play's image paths in
session_history_metadata (thumb, parent_thumb, grandparent_thumb, art), in
the form /library/metadata/<rating key>/thumb/<timestamp>. Its image proxy
takes the rating key from that path, not from the row's rating_key column.

tautulli-remap-rating-keys.py rewrites the key columns of imported (and
healed) rows but leaves these paths alone, so they still name the old
server's keys. The current server hands those keys to unrelated items, and
old plays show the wrong poster -- e.g. Severance displayed with a Star Wars
image because its stored show poster is /library/metadata/1202/..., which is
now a Star Wars clip.

This rewrites only the key segment of each path, using the row's own keys:

  grandparent_thumb   -> grandparent_rating_key (the show)
  parent_thumb        -> parent_rating_key (the season), unless it shares
                         the show poster's key: seasons without a poster
                         store the show's
  thumb               -> rating_key, unless it shares a key claimed above
  art                 -> whichever of the above shares its key (show or
                         season art for episodes, the movie's own for movies)

A path whose key matches none of those is left alone. Rows that already
agree with their keys map onto themselves, so a re-run is a no-op. Content
gone from the library keeps the shifted keys the remap gave it, so its path
404s and Tautulli shows its placeholder poster instead of a stranger's.

Run after tautulli-remap-rating-keys.py. Needs only the database; Plex is
not contacted. Reports what would change and exits without writing unless
--apply is given.

Usage:
  tautulli-fix-image-paths.py [--apply] [options]

Options:
  --apply            write the changes (default: report only)
  --force            with --apply, proceed even if the container is running
  --verbose          list every changed path, not just a summary
  --json             emit the full change set as json
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime

OFFSET = 100_000_000  # tautulli-remap-rating-keys.py's shift for gone content

# Most general first, so a key shared between levels goes to the higher one.
OWNERS = (("grandparent_thumb", "grandparent_rating_key"),
          ("parent_thumb", "parent_rating_key"),
          ("thumb", "rating_key"))
IMAGE_COLUMNS = ("thumb", "parent_thumb", "grandparent_thumb", "art")

ROWS_QUERY = """
SELECT id, media_type, live, rating_key, parent_rating_key,
       grandparent_rating_key, thumb, parent_thumb, grandparent_thumb, art,
       title, grandparent_title
FROM session_history_metadata
ORDER BY id
"""


def config_root(repo_root):
    for line in open(os.path.join(repo_root, ".env")):
        if line.startswith("CONFIG_ROOT="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"error: CONFIG_ROOT not found in {repo_root}/.env")


def path_key(path):
    """The rating key in /library/metadata/<key>/..., or None."""
    parts = (path or "").split("/")
    if len(parts) > 3 and parts[1:3] == ["library", "metadata"] \
            and parts[3].isdigit():
        return parts[3]
    return None


def with_key(path, key):
    parts = path.split("/")
    parts[3] = key
    return "/".join(parts)


def plan_changes(rows):
    """Return {row id: {column: new path}} for paths naming a stale key."""
    changes = {}
    for row in rows:
        if row["live"]:
            continue  # live tv images are channel art, not library items
        stale_to_current = {}
        for column, key_column in OWNERS:
            old = path_key(row[column])
            if old and row[key_column] and old not in stale_to_current:
                stale_to_current[old] = str(row[key_column])
        fixed = {}
        for column in IMAGE_COLUMNS:
            old = path_key(row[column])
            new = stale_to_current.get(old)
            if new and new != old:
                fixed[column] = with_key(row[column], new)
        if fixed:
            changes[row["id"]] = fixed
    return changes


def label(row):
    return row["grandparent_title"] or row["title"] or "(unknown)"


def container_running():
    """True/False if docker can tell us, None if it can't."""
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", "tautulli"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() == "true"


def backup(db_path):
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    dest = os.path.join(
        os.path.dirname(db_path), "backups", f"tautulli.backup-{stamp}.images.db"
    )
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # sqlite's own backup API, so the copy is consistent even mid-write.
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    with out:
        src.backup(out)
    out.close()
    src.close()
    return dest


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    # Status lines go to stderr under --json so stdout stays parseable.
    def notice(message):
        print(message, file=sys.stderr if args.json else sys.stdout)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(config_root(repo_root), "Tautulli/Config/tautulli.db")
    if not os.path.exists(db_path):
        sys.exit(f"error: {db_path} not found")

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(ROWS_QUERY)]
    db.close()

    changes = plan_changes(rows)
    by_id = {r["id"]: r for r in rows}
    paths = sum(len(fixed) for fixed in changes.values())
    gone = [i for i in changes if (by_id[i]["rating_key"] or 0) >= OFFSET]

    if args.json:
        print(json.dumps([
            {"id": i, "title": label(by_id[i]), "column": column,
             "old": by_id[i][column], "new": new}
            for i, fixed in changes.items() for column, new in fixed.items()
        ], indent=2))
    else:
        print(f"{len(rows)} history rows, {len(changes)} with stale image "
              f"paths ({paths} paths)\n")
        for media_type in sorted({by_id[i]["media_type"] for i in changes}):
            ids = [i for i in changes if by_id[i]["media_type"] == media_type]
            items = {label(by_id[i]) for i in ids}
            print(f"  {len(ids):>5} {media_type} rows across {len(items)} "
                  f"titles")
        print(f"  {len(gone):>5} of them are content gone from the library "
              f"(will show tautulli's placeholder)")
        if args.verbose:
            print(f"\n{'id':<7}{'title':<34}{'column':<19}old -> new key")
            print("-" * 80)
            for i, fixed in changes.items():
                for column, new in fixed.items():
                    print(f"{i:<7}{label(by_id[i])[:33]:<34}{column:<19}"
                          f"{path_key(by_id[i][column])} -> {path_key(new)}")

    if not changes:
        notice("\nnothing to do")
        return
    if not args.apply:
        notice(f"\ndry run — no changes written. re-run with --apply to "
               f"update {len(changes)} rows.")
        return

    running = container_running()
    if running is not False and not args.force:
        state = "running" if running else "in an unknown state (docker unavailable)"
        sys.exit(f"error: the tautulli container is {state}. stop it first "
                 f"(docker compose stop tautulli) so it cannot write history "
                 f"mid-update, or pass --force to override.")

    dest = backup(db_path)
    notice(f"\nbacked up to {dest}")
    write = sqlite3.connect(db_path)
    try:
        with write:
            for i, fixed in changes.items():
                write.execute(
                    f"UPDATE session_history_metadata SET "
                    f"{', '.join(f'{column} = ?' for column in fixed)} "
                    f"WHERE id = ?", list(fixed.values()) + [i])
    finally:
        write.close()
    notice(f"updated {paths} image paths on {len(changes)} rows. "
           f"restart tautulli to pick up the change.")


if __name__ == "__main__":
    main()
