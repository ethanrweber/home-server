"""restic's exclude matching, for absolute patterns.

The runner passes every exclude to restic as an absolute pattern, and applies the same
patterns to its own scans (which databases to dump, which archives to warn about) with
this module, so both agree on what is excluded.

Mirrors match() in restic 0.19.1 internal/filter/filter.go: an absolute pattern matches a
path or anything beneath it, "**" stands for any number of directories (including none),
and every other component is a glob. Relative patterns are refused: restic matches those
at any depth, so a bare "cache" would also drop the database dumps.
"""
import fnmatch

_MAGIC = set('*?[\\')


def split(path):
    if not path.startswith('/'):
        raise ValueError(f'not an absolute path or pattern: {path}')
    return ['/'] + [c for c in path.split('/') if c not in ('', '.')]


def _match(pattern, parts):
    if '**' in pattern:
        pos = pattern.index('**')
        for n in range(len(parts) - len(pattern) + 2):
            if _match(pattern[:pos] + ['*'] * n + pattern[pos + 1:], parts):
                return True
        return False
    if len(pattern) > len(parts):
        return False
    for p, s in zip(pattern, parts):
        if _MAGIC.isdisjoint(p):
            if p != s:
                return False
        elif not fnmatch.fnmatchcase(s, p):
            return False
    return True


class Excludes:
    def __init__(self, patterns):
        self.patterns = list(patterns)
        self._split = [split(p) for p in self.patterns]

    def match(self, path):
        parts = split(path)
        return any(_match(p, parts) for p in self._split)


def glob_matches(pattern, path):
    """Whether path is exactly what the glob names (a * stays within one directory)."""
    pattern_parts, parts = split(pattern), split(path)
    return len(pattern_parts) == len(parts) and _match(pattern_parts, parts)


def escape(path):
    """A pattern that matches exactly this literal path (and what is under it)."""
    # a one-character class is literal in both Go's filepath.Match and fnmatch; inside a
    # class Go treats a backslash as an escape, so that one needs doubling
    return ''.join('[\\\\]' if c == '\\' else f'[{c}]' if c in '*?[' else c for c in path)
