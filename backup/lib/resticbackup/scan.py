"""One walk over a set's files, collecting what the dumps and coverage checks need."""
import os
import stat
import time
from dataclasses import dataclass, field

SQLITE_HEADER = b'SQLite format 3\x00'
# a file with one of these names means a database server keeps its data here
DB_SERVER_MARKERS = {'PG_VERSION': 'Postgres', 'aria_log_control': 'MariaDB', 'ibdata1': 'MySQL/MariaDB'}
ARCHIVE_SUFFIXES = ('.zip', '.tar.gz', '.tgz')
RECENT_ARCHIVE_DAYS = 60


@dataclass
class ScanResult:
    sqlite: list = field(default_factory=list)       # SQLite files
    db_servers: list = field(default_factory=list)   # (kind, directory)
    archives: list = field(default_factory=list)     # recent archives under CONFIG_ROOT
    errors: list = field(default_factory=list)       # things that couldn't be read


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip('/') + '/')


def _is_sqlite(path):
    with open(path, 'rb') as f:
        return f.read(16) == SQLITE_HEADER


def scan(paths, excludes, *, config_root, skip=(), now=None):
    """Walk paths (not following symlinks), skipping excluded files and the skip dirs."""
    result = ScanResult()
    cutoff = (now or time.time()) - RECENT_ARCHIVE_DAYS * 86400
    markers = set()

    def visit(path):
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                return
            name = os.path.basename(path)
            if name in DB_SERVER_MARKERS:
                markers.add((DB_SERVER_MARKERS[name], os.path.dirname(path)))
            if (name.endswith(ARCHIVE_SUFFIXES) and st.st_mtime >= cutoff
                    and _inside(path, config_root)):
                result.archives.append(path)
            if st.st_size >= 100 and _is_sqlite(path):
                result.sqlite.append(path)
        except FileNotFoundError:
            pass   # deleted since it was listed (a -journal or -wal, say): nothing to back up
        except OSError as e:
            result.errors.append(f'{path}: {e.strerror or e}')

    def pruned(path):
        return any(_inside(path, s) for s in skip) or excludes.match(path)

    for root in paths:
        if pruned(root):
            continue
        if not os.path.isdir(root) or os.path.islink(root):
            visit(root)
            continue
        def walk_error(e):
            if not isinstance(e, FileNotFoundError):
                result.errors.append(f'{e.filename}: {e.strerror}')

        for dirpath, dirnames, filenames in os.walk(root, onerror=walk_error):
            dirnames[:] = sorted(d for d in dirnames
                                 if not os.path.islink(os.path.join(dirpath, d))
                                 and not pruned(os.path.join(dirpath, d)))
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                if not excludes.match(path):
                    visit(path)

    # report each database server once, at its top-most directory
    for kind, directory in sorted(markers, key=lambda m: len(m[1])):
        if not any(_inside(directory, d) for _, d in result.db_servers):
            result.db_servers.append((kind, directory))
    return result
