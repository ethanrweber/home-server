"""The few things the backup asks Docker."""
import json
import subprocess
from dataclasses import dataclass


@dataclass
class Mount:
    type: str          # bind or volume
    source: str        # host path
    destination: str   # path inside the container
    rw: bool


@dataclass
class Container:
    name: str
    running: bool
    mounts: list


def containers(running_only=False):
    """Every container (or only running ones), with its mounts."""
    ids = subprocess.run(['docker', 'ps', '-q', '--no-trunc'] + ([] if running_only else ['-a']),
                         capture_output=True, text=True, check=True).stdout.split()
    if not ids:
        return []
    inspected = json.loads(subprocess.run(['docker', 'inspect', *ids],
                                          capture_output=True, text=True, check=True).stdout)
    return [Container(c['Name'].lstrip('/'), c['State']['Running'],
                      [Mount(m['Type'], m.get('Source', ''), m['Destination'], m.get('RW', True))
                       for m in c.get('Mounts', [])])
            for c in inspected]


def stop(names):
    if names:
        subprocess.run(['docker', 'stop', *names], check=True)


def start(names):
    if names:
        subprocess.run(['docker', 'start', *names], check=True)
