"""Backup sets: what each one contains, excludes and keeps.

sets/sets.conf names every set. For a set called X:
  sets/X.paths          paths to back up, one per line
  sets/X.exclude        restic exclude patterns (optional, must be absolute)
  sets/X.canary         files that every snapshot of X must contain (optional, globs)
  /etc/restic/X.local.paths   extra paths for this machine only (optional, not in git)

Lines may use ${REPO_ROOT}, ${CONFIG_ROOT}, ${MEDIA_ROOT} and ${DUMPS}. Blank lines and
lines starting with # are ignored.

Each file belongs to one set: a path listed by another set is excluded from any set
whose paths contain it (so the app backup folders go in appzips, not in stack).
"""
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .config import ConfigError
from .patterns import escape, split

_NAME = re.compile(r'^[a-z0-9][a-z0-9-]*$')
_PLACEHOLDER = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')


@dataclass
class BackupSet:
    name: str
    dump: bool                 # dump the SQLite databases found in this set
    retention: list            # restic forget flags
    paths: list = field(default_factory=list)
    excludes: list = field(default_factory=list)
    canary: list = field(default_factory=list)

    def dump_dir(self, dumps_root):
        return str(Path(dumps_root) / self.name)


def expand(text, placeholders, where):
    def sub(m):
        if m.group(1) not in placeholders:
            raise ConfigError(f'{where}: unknown placeholder ${{{m.group(1)}}}')
        return placeholders[m.group(1)]
    return _PLACEHOLDER.sub(sub, text)


def read_list(path, placeholders, absolute=True):
    path = Path(path)
    if not path.exists():
        return []
    items = []
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        value = expand(line, placeholders, f'{path}:{n}')
        if absolute and not value.startswith('/'):
            raise ConfigError(f'{path}:{n}: {value} is not absolute')
        items.append(value.rstrip('/') or '/')
    return items


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip('/') + '/')


def load_sets(sets_dir, etc_dir, placeholders):
    """Every set defined in sets.conf, with its lists expanded."""
    sets_dir = Path(sets_dir)
    conf = sets_dir / 'sets.conf'
    defined = {}
    for n, raw in enumerate(conf.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        words = shlex.split(line)
        if len(words) < 3 or not _NAME.match(words[0]) or words[1] not in ('yes', 'no'):
            raise ConfigError(f'{conf}:{n}: expected "<name> <dump yes|no> <forget flags>"')
        flags = [w for w in words[2:] if w.startswith('-')]
        if not words[2].startswith('--keep') or not all(f.startswith('--keep') for f in flags):
            raise ConfigError(f'{conf}:{n}: retention must be restic --keep-* flags')
        s = BackupSet(words[0], words[1] == 'yes', words[2:])
        s.paths = (read_list(sets_dir / f'{s.name}.paths', placeholders)
                   + read_list(Path(etc_dir) / f'{s.name}.local.paths', placeholders))
        if not s.paths:
            raise ConfigError(f'set {s.name} has no paths')
        if s.dump:
            s.paths.append(s.dump_dir(placeholders['DUMPS']))
        s.excludes = read_list(sets_dir / f'{s.name}.exclude', placeholders)
        s.canary = read_list(sets_dir / f'{s.name}.canary', placeholders)
        for p in s.excludes:
            split(p)  # raises on a relative pattern
        defined[s.name] = s

    for s in defined.values():
        for other in defined.values():
            if other is s:
                continue
            for p in other.paths:
                if any(_inside(p, mine) and p != mine for mine in s.paths):
                    s.excludes.append(escape(p))
    return defined


def select(defined, names):
    missing = [n for n in names if n not in defined]
    if missing:
        raise ConfigError(f'SETS names undefined set(s): {" ".join(missing)}')
    return [defined[n] for n in names]


def existing_paths(backup_set):
    """(paths that exist, paths that don't)."""
    present = [p for p in backup_set.paths if os.path.exists(p)]
    return present, [p for p in backup_set.paths if p not in present]
