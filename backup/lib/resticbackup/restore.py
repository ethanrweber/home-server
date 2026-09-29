"""restore-db: put database copies from a snapshot back in place.

  sudo restore-db sonarr                    Sonarr's database from the latest snapshot
  sudo restore-db --snapshot 1a2b3c4d komga from a particular snapshot
  sudo restore-db --set stack --all         every database in the stack set (VM rebuild)

A NAME matches, ignoring case, anywhere in a database's path under its root: "sonarr"
matches Sonarr/Config/sonarr.db. Each copy is read from the snapshot and checked before
anything is touched. Then the containers using that folder are stopped, the current
database and its -wal/-shm files are moved aside (never deleted), the copy goes in with
its original owner and mode, and the containers are started again.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime

from . import config, docker, dumps, lock, sets
from .config import ConfigError
from .restic import Restic, ResticError


def _latest(restic, host, tag):
    snaps = json.loads(restic.run('snapshots', '--json', '--host', host, '--tag', tag,
                                  '--latest', '1').stdout or '[]')
    # --latest 1 returns one snapshot per path group; take the newest (times carry nanoseconds
    # and a UTC offset, so compare them parsed rather than as strings)
    newest = max(snaps, key=lambda s: datetime.fromisoformat(re.sub(r'\.\d+', '', s['time'])), default=None)
    return newest['id'] if newest else None


def _read(restic, snapshot, path):
    with tempfile.TemporaryFile() as f:
        restic.dump(snapshot, path, f)
        f.seek(0)
        return f.read()


def _users(target):
    """Running containers that can write to the database's folder."""
    folder = os.path.dirname(target)
    return sorted({c.name for c in docker.containers(running_only=True) for m in c.mounts
                   if m.type == 'bind' and m.rw
                   and (folder == m.source or folder.startswith(m.source.rstrip('/') + '/'))})


def main(argv):
    parser = argparse.ArgumentParser(prog='restore-db', description=__doc__.split('\n\n')[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__.split('\n\n', 1)[1])
    parser.add_argument('names', nargs='*', metavar='NAME')
    parser.add_argument('--all', action='store_true', help='every database in --set')
    parser.add_argument('--set', help='only look in this set (stack, media, ...)')
    parser.add_argument('--repo', default='b2', help='repository to restore from (default b2)')
    parser.add_argument('--snapshot', default='latest', help='snapshot ID (default: latest)')
    parser.add_argument('--yes', action='store_true', help="don't ask for confirmation")
    args = parser.parse_args(argv)
    if bool(args.names) == args.all:
        parser.error('give database names, or --all')
    if args.all and not args.set:
        parser.error('--all needs --set: restoring every set would also roll back live data '
                     'you may not mean to, such as the book library')
    if os.geteuid() != 0 and not os.environ.get('RB_ALLOW_NONROOT'):
        print('restore-db must run as root (sudo)', file=sys.stderr)
        return 2
    try:
        return _restore(args)
    except (ConfigError, ResticError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1
    except (OSError, subprocess.CalledProcessError) as e:
        print(f'could not ask Docker which containers use these files: {e}', file=sys.stderr)
        return 1


def _restore(args):
    cfg = config.load(args.repo)
    restic = Restic(cfg.restic_env(), config.CACHE)
    candidates = [s for s in sets.select(sets.load_sets(config.LIB / 'sets', config.ETC, cfg.placeholders),
                                         cfg.sets) if s.dump]
    if args.set:
        candidates = [s for s in candidates if s.name == args.set]
        if not candidates:
            raise ConfigError(f'{args.repo} has no set {args.set} with database copies')

    chosen = []   # (snapshot, Dump fields, target path)
    for s in candidates:
        snapshot = args.snapshot if args.snapshot != 'latest' else _latest(restic, cfg.host, s.name)
        if not snapshot:
            print(f'{s.name}: no snapshots yet')
            continue
        try:
            manifest = json.loads(_read(restic, snapshot, str(config.DUMPS / s.name / dumps.MANIFEST)))
        except (ResticError, ValueError):
            continue   # not a snapshot of this set
        for d in manifest['dumps']:
            if args.all or any(n.lower() in d['relative'].lower() for n in args.names):
                root = cfg.roots.get(d['root'])
                chosen.append((snapshot, d, os.path.join(root, d['relative']) if root else d['source']))
    if not chosen:
        print('no matching database copies found')
        return 1

    containers = sorted({name for _, _, target in chosen for name in _users(target)})
    print('restoring:')
    for snapshot, d, target in chosen:
        print(f"  {target}  ({d['size'] / 1e6:.1f} MB, snapshot {snapshot[:8]})")
    print('containers stopped while restoring: ' + (' '.join(containers) or 'none'))
    stamp = time.strftime('%Y%m%d-%H%M%S')
    print(f'current files are kept as <name>.pre-restore-{stamp}')
    if not args.yes and input('type yes to continue: ').strip() != 'yes':
        print('nothing changed')
        return 1
    with lock.held():   # not while a backup is copying these databases
        return _put_back(restic, chosen, containers, stamp)


def _put_back(restic, chosen, containers, stamp):
    staged = []
    try:
        for snapshot, d, target in chosen:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = f'{target}.restore-tmp'
            staged.append((tmp, target))
            with open(tmp, 'wb') as f:
                restic.dump(snapshot, d['dump'], f)
            if os.path.getsize(tmp) != d['size']:
                raise ValueError(f"{tmp} is {os.path.getsize(tmp)} bytes, expected {d['size']}")
            check = sqlite3.connect(tmp)
            try:
                result = check.execute('PRAGMA integrity_check').fetchall()
            finally:
                check.close()
            if result != [('ok',)]:
                raise ValueError(f'integrity_check on {tmp}: {result[:3]}')
            os.chown(tmp, d['uid'], d['gid'])
            os.chmod(tmp, int(d['mode'], 8))
            print(f'  checked {target}')
    except (OSError, ValueError, sqlite3.Error, ResticError) as e:
        for tmp, _ in staged:
            if os.path.exists(tmp):
                os.unlink(tmp)
        print(f'stopped before changing anything: {e}', file=sys.stderr)
        return 1

    try:
        docker.stop(containers)
    except (OSError, subprocess.CalledProcessError) as e:
        for tmp, _ in staged:
            os.unlink(tmp)
        docker.start(containers)
        print(f'could not stop {" ".join(containers)}, so nothing was changed: {e}', file=sys.stderr)
        return 1
    done = []
    try:
        for tmp, target in staged:
            for suffix in ('',) + dumps.SIDE_FILES:
                if os.path.exists(target + suffix):
                    os.rename(target + suffix, f'{target}{suffix}.pre-restore-{stamp}')
            os.replace(tmp, target)
            done.append(target)
            print(f'  restored {target}')
    except OSError as e:
        print(f'stopped partway ({e}). restored: {", ".join(done) or "nothing"}. the originals '
              f'of anything touched are the *.pre-restore-{stamp} files', file=sys.stderr)
        return 1
    finally:
        docker.start(containers)
    print(f'done. to undo, stop {" ".join(containers) or "the app"} and move the '
          f'*.pre-restore-{stamp} files back over the restored ones')
    return 0
