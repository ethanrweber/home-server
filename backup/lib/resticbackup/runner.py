"""restic-backup: back up to, maintain and inspect one restic repository.

  restic-backup backup <repo> [--dry-run]   back up every set the repository receives
  restic-backup maint <repo>                prune and check, when due
  restic-backup show <repo>                 print what each set backs up, change nothing
  restic-backup restic <repo> <args...>     run any restic command against the repository

<repo> names /etc/restic/<repo>.env. Every command must run as root.
"""
import argparse
import os
import shutil
import sys
import time
import traceback
from datetime import datetime

from . import config, coverage, dumps, healthchecks, lock, scan, sets
from .config import ConfigError
from .patterns import Excludes, glob_matches
from .restic import EXIT_MEANINGS, Restic, ResticError

DAY = 86400


class Log:
    """Prints to the journal, and keeps a copy for the healthchecks ping."""

    def __init__(self):
        self.lines = []

    def __call__(self, line=''):
        print(line, flush=True)
        self.lines.append(line)


def _placeholder_list(path, cfg):
    return [sets.expand(entry, cfg.placeholders, str(path)) for entry in coverage.read_allow(path)]


# --- backup ---------------------------------------------------------------------------

def backup(repo, dry_run=False):
    log = Log()
    local = []   # problems on this machine: every repository's backup is incomplete
    remote = []  # problems with this repository only
    cfg = None
    try:
        cfg = config.load(repo)
        restic = Restic(cfg.restic_env(), config.CACHE, log)
        log(f'backing up to {repo}' + (' (dry run: nothing is uploaded)' if dry_run else ''))
        if not dry_run:
            healthchecks.ping(cfg.repo_env.get('HC_BACKUP_URL'), 'start', log=log)
        with lock.held(log):
            _backup_sets(cfg, restic, log, local, remote, dry_run)
    except ConfigError as e:
        local.append(f'configuration: {e}')
    except Exception:
        local.append('unexpected error:\n' + traceback.format_exc())

    problems = local + remote
    summary = [f'{repo}: ' + (f'{len(problems)} problem(s)' if problems else 'ok')]
    summary += [f'  PROBLEM: {p}' for p in problems]
    log()
    for line in summary:
        log(line)
    if not dry_run:
        body = '\n'.join(summary + ['', '--- log ---'] + log.lines)
        if local:
            # a local problem means every repository is missing the same data
            for url in config.all_backup_urls().values():
                healthchecks.ping(url, 'fail', body, log=log)
        elif cfg:
            healthchecks.ping(cfg.repo_env.get('HC_BACKUP_URL'), 'fail' if remote else '', body, log=log)
    return 1 if problems else 0


def _backup_sets(cfg, restic, log, local, remote, dry_run):
    defined = sets.load_sets(config.LIB / 'sets', config.ETC, cfg.placeholders)
    active = sets.select(defined, cfg.sets)
    allowed_errors = Excludes(_placeholder_list(config.LIB / 'read-errors.allow', cfg))

    if not dry_run:
        try:
            restic.unlock()
        except ResticError as e:
            remote.append(str(e))
            return   # the repository can't be reached; nothing below would work

    local += coverage.volumes(coverage.read_allow(config.LIB / 'volumes.allow'))

    if len(config.DUMPS.parts) < 3:
        raise ConfigError(f'refusing to empty {config.DUMPS}')
    shutil.rmtree(config.DUMPS, ignore_errors=True)
    config.DUMPS.mkdir(mode=0o700, parents=True)

    found = {}
    for s in active:
        log()
        log(f'== {s.name}')
        excludes = list(s.excludes)
        copied = []
        if s.dump:
            result = scan.scan([p for p in s.paths if os.path.exists(p)], Excludes(s.excludes),
                               config_root=cfg.placeholders['CONFIG_ROOT'], skip=[str(config.DUMPS)])
            local += coverage.from_scan(s.name, result)
            found[s.name] = result.sqlite
            copied, generated, problems = dumps.dump_all(s.name, result.sqlite, config.DUMPS, cfg.roots, log)
            local += problems
            excludes += generated

        present, missing = sets.existing_paths(s)
        local += [f'{s.name}: {p} does not exist' for p in missing]
        if not present:
            continue

        res = restic.backup(cfg.host, s.name, present, excludes, dry_run)
        incomplete = False
        if res.exit_code == 3:
            unexpected = [(p, m) for p, m in res.errors
                          if not (p.startswith('/') and allowed_errors.match(p))]
            if unexpected or not res.errors:
                incomplete = True
                local.append(f'{s.name}: restic could not read '
                             + (f'{len(unexpected)} file(s): ' + '; '.join(f'{p}: {m}' for p, m in unexpected[:20])
                                if unexpected else f'some files and did not say which: {res.stderr[-500:]}'))
            else:
                log(f'  {len(res.errors)} read error(s), all on read-errors.allow')
        elif res.exit_code != 0:
            meaning = EXIT_MEANINGS.get(res.exit_code, 'failed')
            remote.append(f'{s.name}: restic backup exited {res.exit_code} ({meaning}): '
                          f'{res.stderr.strip()[-1500:]}')
            continue

        sm = res.summary
        log(f"  {'would add' if dry_run else 'added'} {sm.get('data_added', 0) / 1e6:.1f} MB: "
            f"{sm.get('files_new', 0)} new, {sm.get('files_changed', 0)} changed, "
            f"{sm.get('files_unmodified', 0)} unchanged files"
            + (f", snapshot {res.snapshot_id[:8]}" if res.snapshot_id else ''))
        if dry_run:
            continue
        if not res.snapshot_id:
            remote.append(f'{s.name}: restic reported no snapshot')
            continue
        try:
            missing = _check_snapshot(restic, res.snapshot_id, s, copied)
            local += missing
            if incomplete or missing:
                # keep every older snapshot: for appzips (--keep-last 1) forgetting would
                # replace the last complete one with this one
                log(f'  kept all older {s.name} snapshots, since this one is incomplete')
            else:
                restic.forget(cfg.host, s.name, s.retention)
        except ResticError as e:
            remote.append(f'{s.name}: {e}')

    local += coverage.vanished_databases(config.STATE / 'databases.json', found)
    if not dry_run:
        coverage.save_databases(config.STATE / 'databases.json', found)


def _check_snapshot(restic, snapshot_id, s, copied):
    """Open the new snapshot and confirm the files that matter are really in it."""
    nodes = {n['path']: n for n in restic.ls(snapshot_id)}
    short = snapshot_id[:8]
    problems = []
    for d in copied:
        node = nodes.get(d.dump)
        if node is None:
            problems.append(f'{s.name}: snapshot {short} is missing the copy of {d.source}')
        elif node.get('size') != d.size:
            problems.append(f"{s.name}: snapshot {short} has {d.dump} at {node.get('size')} bytes, "
                            f'expected {d.size}')
    if s.dump and str(config.DUMPS / s.name / dumps.MANIFEST) not in nodes:
        problems.append(f'{s.name}: snapshot {short} is missing the dump manifest')
    for pattern in s.canary:
        if not any(n.get('type') == 'file' and n.get('size', 0) > 0 and glob_matches(pattern, path)
                   for path, n in nodes.items()):
            problems.append(f'{s.name}: snapshot {short} has no non-empty file matching {pattern}')
    return problems


# --- maintenance ----------------------------------------------------------------------

def maint(repo):
    log = Log()
    problems = []
    cfg = None
    try:
        cfg = config.load(repo)
        restic = Restic(cfg.restic_env(), config.CACHE, log)
        healthchecks.ping(cfg.repo_env.get('HC_MAINT_URL'), 'start', log=log)
        with lock.held(log):
            problems += _maintain(repo, restic, log)
    except ConfigError as e:
        problems.append(f'configuration: {e}')
    except ResticError as e:
        problems.append(str(e))
    except Exception:
        problems.append('unexpected error:\n' + traceback.format_exc())

    log()
    log(f'{repo} maintenance: ' + (f'{len(problems)} problem(s)' if problems else 'ok'))
    for p in problems:
        log(f'  PROBLEM: {p}')
    healthchecks.ping(cfg.repo_env.get('HC_MAINT_URL') if cfg else _maint_url(repo),
                      'fail' if problems else '', '\n'.join(log.lines), log=log)
    return 1 if problems else 0


def _maint_url(repo):
    """HC_MAINT_URL when the configuration didn't load, if the repository's file is readable."""
    try:
        return config.parse_env_file(config.ETC / f'{repo}.env').get('HC_MAINT_URL')
    except (OSError, ConfigError):
        return None


def _maintain(repo, restic, log):
    """Weekly prune and check; each month, also read back a different 1/12 of the data."""
    stamps = config.STATE / repo
    stamps.mkdir(parents=True, exist_ok=True)
    now = datetime.now()

    def due(name, days):
        # half a day of slack, so a timer firing a few minutes early doesn't slip a whole day
        stamp = stamps / name
        return not stamp.exists() or time.time() - stamp.stat().st_mtime >= (days - 0.5) * DAY

    def done(name, content=''):
        (stamps / name).write_text(content)

    problems = []
    ran = []
    restic.unlock()
    if due('prune', 7):
        ran.append('prune')
        log('restic prune')
        if restic.stream('prune') == 0:
            done('prune')
        else:
            problems.append('restic prune failed; see the log')

    month = now.strftime('%Y-%m')
    read_stamp = stamps / 'read-data'
    if not read_stamp.exists() or read_stamp.read_text() != month:
        subset = f'{now.month}/12'
        ran.append('check')
        log(f'restic check --read-data-subset={subset}')
        if restic.stream('check', f'--read-data-subset={subset}') == 0:
            done('read-data', month)
            done('check')
        else:
            problems.append(f'restic check --read-data-subset={subset} failed; see the log')
    elif due('check', 7):
        ran.append('check')
        log('restic check')
        if restic.stream('check') == 0:
            done('check')
        else:
            problems.append('restic check failed; see the log')

    if not ran:
        log('nothing due')
    return problems


# --- show -----------------------------------------------------------------------------

def show(repo):
    cfg = config.load(repo)
    defined = sets.load_sets(config.LIB / 'sets', config.ETC, cfg.placeholders)
    print(f'repository {repo}: {cfg.repo_env["RESTIC_REPOSITORY"]}')
    print(f'host: {cfg.host}')
    for s in sets.select(defined, cfg.sets):
        print(f'\n== {s.name}   retention: {" ".join(s.retention)}'
              + ('   (SQLite databases are copied, then their live files excluded)' if s.dump else ''))
        print('paths:')
        for p in s.paths:
            print(f'  {p}' + ('' if os.path.exists(p) or p.startswith(str(config.DUMPS)) else '   MISSING'))
        if s.excludes:
            print('excludes:')
            for p in s.excludes:
                print(f'  {p}')
        if s.canary:
            print('every snapshot must contain:')
            for p in s.canary:
                print(f'  {p}')
    print('\nread errors tolerated (read-errors.allow):')
    for p in _placeholder_list(config.LIB / 'read-errors.allow', cfg) or ['(none)']:
        print(f'  {p}')
    print('Docker volumes allowed (volumes.allow):')
    for p in coverage.read_allow(config.LIB / 'volumes.allow') or ['(none)']:
        print(f'  {p}')
    return 0


# --- entry point ----------------------------------------------------------------------

def main(argv):
    parser = argparse.ArgumentParser(prog='restic-backup', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('backup', help='back up every set the repository receives')
    p.add_argument('repo')
    p.add_argument('--dry-run', action='store_true',
                   help='scan and copy databases, and show what restic would upload; change nothing in the repository')
    sub.add_parser('maint', help='prune and check, when due').add_argument('repo')
    sub.add_parser('show', help='print what each set backs up').add_argument('repo')
    p = sub.add_parser('restic', help='run a restic command against the repository')
    p.add_argument('repo')
    p.add_argument('args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    if os.geteuid() != 0 and not os.environ.get('RB_ALLOW_NONROOT'):
        print('restic-backup must run as root (sudo): many of the files it backs up are root-only',
              file=sys.stderr)
        return 2
    try:
        if args.command == 'backup':
            return backup(args.repo, args.dry_run)
        if args.command == 'maint':
            return maint(args.repo)
        if args.command == 'show':
            return show(args.repo)
        cfg = config.load(args.repo)
        Restic(cfg.restic_env(), config.CACHE).exec(args.args)
    except ConfigError as e:
        print(f'configuration: {e}', file=sys.stderr)
        return 2
