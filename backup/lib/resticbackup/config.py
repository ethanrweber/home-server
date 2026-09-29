"""Where things live, and the config files the runner reads.

Every location can be overridden with an RB_* environment variable. Only the tests do
that; the installed runner always uses the defaults below.
"""
import os
import re
from dataclasses import dataclass
from pathlib import Path

# the install dir (or backup/ in a checkout): holds sets/ and the *.allow files
LIB = Path(os.environ.get('RB_LIB', Path(__file__).resolve().parents[2]))
ETC = Path(os.environ.get('RB_ETC', '/etc/restic'))
STATE = Path(os.environ.get('RB_STATE', '/var/lib/restic-backup'))
DUMPS = Path(os.environ.get('RB_DUMPS', '/var/lib/restic-dumps'))
CACHE = Path(os.environ.get('RB_CACHE', '/var/lib/restic-cache'))
LOCK = Path(os.environ.get('RB_LOCK', '/run/lock/restic-backup.lock'))

REPO_NAME = re.compile(r'^[a-z0-9][a-z0-9-]*$')
# keys in /etc/restic/<repo>.env that are for the runner, not for restic
RUNNER_KEYS = {'SETS', 'HC_BACKUP_URL', 'HC_MAINT_URL'}


class ConfigError(Exception):
    pass


_ENV_LINE = re.compile(r'^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$')


def parse_env_file(path):
    """KEY=VALUE lines, optionally quoted. No variable expansion, no inline comments."""
    values = {}
    for n, raw in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            raise ConfigError(f'{path}:{n}: expected KEY=VALUE')
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        values[key] = value
    return values


def read_stack_roots(repo_root):
    """CONFIG_ROOT and MEDIA_ROOT from the stack's .env.

    The file is never sourced (some values hold shell metacharacters); only these two
    lines are read, the same way docker compose reads them.
    """
    env_path = Path(repo_root) / '.env'
    roots = {}
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        for key in ('CONFIG_ROOT', 'MEDIA_ROOT'):
            if line.startswith(key + '='):
                value = line[len(key) + 1:].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                    value = value[1:-1]
                else:
                    value = value.split(' #', 1)[0].strip()
                roots[key] = value
    for key in ('CONFIG_ROOT', 'MEDIA_ROOT'):
        value = roots.get(key)
        if not value:
            raise ConfigError(f'{env_path}: {key} is not set')
        if not os.path.isabs(value) or not os.path.isdir(value):
            raise ConfigError(f'{env_path}: {key}={value} is not an existing absolute directory')
        roots[key] = value.rstrip('/') or '/'
    return roots


@dataclass
class Config:
    repo: str                  # repository name, e.g. b2
    repo_env: dict             # /etc/restic/<repo>.env
    host: str                  # the --host every snapshot is recorded under
    placeholders: dict         # ${NAME} -> value, for the set lists
    sets: list                 # names of the sets this repository receives

    @property
    def roots(self):
        """The placeholders a backed-up file can live under, for dump manifests."""
        return {k: self.placeholders[k] for k in ('REPO_ROOT', 'CONFIG_ROOT', 'MEDIA_ROOT')}

    def restic_env(self):
        """The repository's settings, minus the runner's own keys."""
        return {k: v for k, v in self.repo_env.items() if k not in RUNNER_KEYS}


def load(repo):
    if not REPO_NAME.match(repo):
        raise ConfigError(f'bad repository name {repo!r}')
    conf_path = ETC / 'backup.conf'
    if not conf_path.exists():
        raise ConfigError(f'{conf_path} is missing; run backup/install.sh')
    conf = parse_env_file(conf_path)
    repo_root = conf.get('REPO_ROOT', '')
    host = conf.get('BACKUP_HOST', '')
    if not os.path.isabs(repo_root) or not (Path(repo_root) / '.env').is_file():
        raise ConfigError(f'{conf_path}: REPO_ROOT={repo_root} is not a checkout with a .env')
    if not host:
        raise ConfigError(f'{conf_path}: BACKUP_HOST is not set')

    env_path = ETC / f'{repo}.env'
    if not env_path.exists():
        raise ConfigError(f'{env_path} is missing; copy backup/etc/{repo}.env.example')
    repo_env = parse_env_file(env_path)
    for key in ('RESTIC_REPOSITORY', 'RESTIC_PASSWORD_FILE', 'SETS'):
        if not repo_env.get(key):
            raise ConfigError(f'{env_path}: {key} is not set')
    if not Path(repo_env['RESTIC_PASSWORD_FILE']).is_file():
        raise ConfigError(f"{env_path}: RESTIC_PASSWORD_FILE {repo_env['RESTIC_PASSWORD_FILE']} is missing")

    placeholders = {'REPO_ROOT': repo_root.rstrip('/'), 'DUMPS': str(DUMPS)}
    placeholders.update(read_stack_roots(repo_root))
    return Config(repo, repo_env, host, placeholders, repo_env['SETS'].split())


def all_backup_urls():
    """HC_BACKUP_URL of every configured repository, for failures that aren't the target's fault."""
    urls = {}
    for path in sorted(ETC.glob('*.env')):
        try:
            url = parse_env_file(path).get('HC_BACKUP_URL')
        except (OSError, ConfigError):
            continue
        if url:
            urls[path.stem] = url
    return urls
