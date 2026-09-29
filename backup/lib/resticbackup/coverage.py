"""Checks for data the backup would otherwise miss without any error.

Each check returns a list of problems. A problem fails the run, but never stops it: the
rest is still backed up.
"""
import json
import os
import subprocess
from pathlib import Path

from . import docker


def read_allow(path):
    """Entries from an allowlist file; anything after # is a comment."""
    path = Path(path)
    if not path.exists():
        return []
    entries = []
    for raw in path.read_text().splitlines():
        entry = raw.split('#', 1)[0].strip()
        if entry:
            entries.append(entry)
    return entries


def volumes(allowed):
    """Containers that keep data in a Docker volume, which no set can see."""
    try:
        found = docker.containers()
    except (OSError, subprocess.CalledProcessError, ValueError) as e:
        return [f'could not list Docker containers: {e}']
    problems = []
    for c in found:
        for m in c.mounts:
            if m.type == 'volume' and f'{c.name}:{m.destination}' not in allowed:
                problems.append(
                    f'container {c.name} keeps {m.destination} in a Docker volume, which no set '
                    f'covers. Bind-mount it under CONFIG_ROOT instead, or add '
                    f'"{c.name}:{m.destination}" to volumes.allow')
    return problems


def from_scan(set_name, result):
    problems = [f'{set_name}: could not read {e}' for e in result.errors]
    for kind, directory in result.db_servers:
        problems.append(
            f'{set_name}: {directory} looks like a {kind} data directory. A copy of a running '
            f'database server is not safe to restore; it needs its own dump step')
    for archive in result.archives:
        problems.append(
            f'{set_name}: {archive} is a recent archive outside the app backup folders. If an '
            f'app writes its backups there, add the folder to sets/appzips.paths; otherwise '
            f'exclude it')
    return problems


def vanished_databases(state_file, found):
    """Databases found last run that no longer exist. Reported once, on the next run."""
    state_file = Path(state_file)
    previous = json.loads(state_file.read_text()) if state_file.exists() else {}
    problems = []
    for set_name, sources in previous.items():
        if set_name not in found:
            continue
        for src in sources:
            if src not in found[set_name] and not os.path.exists(src):
                problems.append(
                    f'{set_name}: database {src} was backed up last run and is gone now '
                    f'(reported once; if its app was removed on purpose, nothing to do)')
    return problems


def save_databases(state_file, found):
    state_file = Path(state_file)
    previous = json.loads(state_file.read_text()) if state_file.exists() else {}
    previous.update(found)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(previous, indent=2, sort_keys=True) + '\n')
