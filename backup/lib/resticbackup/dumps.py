"""Consistent copies of live SQLite databases.

Copying a database file while its app writes to it can capture a half-written state, so
each one found in a set is copied with SQLite's backup API instead, verified, and stored
under ${DUMPS}/<set>/<its original absolute path>. Only after a copy succeeds is the live
file (and its -wal/-shm/-journal) excluded from the snapshot; if a copy fails, the live
file is backed up as-is and the run is reported as failed.

manifest.json next to the copies records where each came from, its owner and mode, so
restore-db can put it back.
"""
import json
import os
import sqlite3
import stat
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path

from .patterns import escape

MANIFEST = 'manifest.json'
SIDE_FILES = ('-wal', '-shm', '-journal')


@dataclass
class Dump:
    source: str      # the live database
    dump: str        # the copy
    root: str        # the placeholder the source lives under (CONFIG_ROOT, ...), or ''
    relative: str    # source relative to that root (or the absolute source)
    size: int
    uid: int
    gid: int
    mode: str        # octal, e.g. "0644"


def copy_database(src, dst, timeout=60):
    """Copy src to dst in one read transaction, then check the copy."""
    tmp = f'{dst}.tmp'
    if os.path.exists(tmp):
        os.unlink(tmp)
    uri = 'file:' + urllib.parse.quote(src) + '?mode=ro'
    try:
        source = sqlite3.connect(uri, uri=True, timeout=timeout)
        try:
            target = sqlite3.connect(tmp)
            try:
                source.backup(target, pages=-1)
                result = target.execute('PRAGMA quick_check').fetchall()
            finally:
                target.close()
        finally:
            source.close()
        if result != [('ok',)]:
            raise sqlite3.DatabaseError(f'quick_check on the copy: {result[:3]}')
        st = os.stat(src)
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        os.replace(tmp, dst)
    except BaseException:
        # a half-written copy must not end up in the snapshot
        for leftover in (tmp, f'{tmp}-wal', f'{tmp}-shm', f'{tmp}-journal'):
            if os.path.exists(leftover):
                os.unlink(leftover)
        raise
    return st


def _root_of(path, roots):
    """The innermost root containing path, and path relative to it."""
    matches = [(len(r), name, r) for name, r in roots.items() if path.startswith(r.rstrip('/') + '/')]
    if not matches:
        return '', path
    _, name, root = max(matches)
    return name, os.path.relpath(path, root)


def dump_all(set_name, databases, dumps_root, roots, log=print):
    """Copy every database; return (dumps, excludes for the copied originals, problems)."""
    set_dir = Path(dumps_root) / set_name
    set_dir.mkdir(parents=True, exist_ok=True)
    dumps, excludes, problems = [], [], []
    for src in databases:
        dst = set_dir / src.lstrip('/')
        dst.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            st = copy_database(src, str(dst))
        except (sqlite3.Error, OSError) as e:
            problems.append(f'{set_name}: could not copy database {src}: {e}')
            continue
        root, relative = _root_of(src, roots)
        dumps.append(Dump(src, str(dst), root, relative, os.path.getsize(dst),
                          st.st_uid, st.st_gid, f'{stat.S_IMODE(st.st_mode):04o}'))
        excludes += [escape(src + suffix) for suffix in ('',) + SIDE_FILES]
        log(f'  copied {src} ({os.path.getsize(dst) / 1e6:.1f} MB, {time.monotonic() - started:.1f}s)')
    manifest = {'set': set_name, 'created': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'dumps': [asdict(d) for d in dumps]}
    (set_dir / MANIFEST).write_text(json.dumps(manifest, indent=2) + '\n')
    return dumps, excludes, problems
