"""Running restic against one repository."""
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field

# restic's documented exit codes
EXIT_MEANINGS = {1: 'failed', 3: 'some source files could not be read',
                 10: 'repository does not exist', 11: 'could not lock the repository',
                 12: 'wrong password', 130: 'interrupted'}


class ResticError(Exception):
    def __init__(self, command, code, stderr):
        meaning = EXIT_MEANINGS.get(code, 'failed')
        super().__init__(f'restic {command} exited {code} ({meaning}): {stderr.strip()[-2000:]}')
        self.code = code


@dataclass
class BackupResult:
    exit_code: int = 0
    snapshot_id: str = ''
    errors: list = field(default_factory=list)   # (path, message) for files restic couldn't read
    summary: dict = field(default_factory=dict)
    stderr: str = ''                              # anything on stderr that wasn't a read error


class Restic:
    def __init__(self, repo_env, cache_dir, log=print):
        # nothing restic- or credential-related leaks in from the caller's environment
        env = {k: v for k, v in os.environ.items() if not k.startswith(('RESTIC_', 'AWS_', 'B2_'))}
        env.update(repo_env)
        # systemd gives root services no $HOME, and restic 0.19 aborts without a cache dir
        env.setdefault('RESTIC_CACHE_DIR', str(cache_dir))
        self.env = env
        self.log = log

    def run(self, *args, check=True):
        """Run restic and capture its output."""
        proc = subprocess.run(['restic', *args], env=self.env, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise ResticError(args[0], proc.returncode, proc.stderr)
        return proc

    def stream(self, *args):
        """Run restic with its output going straight to the log; return the exit code."""
        return subprocess.run(['restic', *args], env=self.env).returncode

    def exec(self, args):
        os.execvpe('restic', ['restic', *args], self.env)

    def unlock(self):
        """Remove stale locks only (a crashed or interrupted run leaves one behind)."""
        proc = self.run('unlock')
        text = (proc.stdout + proc.stderr).strip()
        if text:
            self.log(f'  restic unlock: {text}')

    def backup(self, host, tag, paths, excludes, dry_run=False):
        result = BackupResult()
        with tempfile.NamedTemporaryFile('w', prefix='restic-paths-') as paths_file, \
                tempfile.TemporaryFile('w+') as stderr:
            paths_file.write(''.join(p + '\n' for p in paths))
            paths_file.flush()
            args = ['restic', 'backup', '--json', '--host', host, '--tag', tag,
                    '--files-from-verbatim', paths_file.name]
            for pattern in excludes:
                args += ['--exclude', pattern]
            if dry_run:
                args.append('--dry-run')
            env = dict(self.env, RESTIC_PROGRESS_FPS=str(1 / 60))   # a progress line a minute
            proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=stderr, text=True)
            # with --json, progress and the summary go to stdout; errors go to stderr
            for line in proc.stdout:
                self._handle(line, result)
            result.exit_code = proc.wait()
            stderr.seek(0)
            texts = [self._handle(line, result) for line in stderr.read().splitlines()]
            result.stderr = '\n'.join(t for t in texts if t)
        return result

    def _handle(self, line, result):
        """One line of restic's --json output. Returns what belongs in an error report."""
        try:
            msg = json.loads(line)
        except ValueError:
            msg = None
        if not isinstance(msg, dict):
            if line.strip():
                self.log(f'  {line.rstrip()}')
            return line.strip()
        kind = msg.get('message_type')
        if kind == 'status' and msg.get('total_bytes'):
            self.log(f"  {msg.get('percent_done', 0):.0%} of {msg['total_bytes'] / 1e6:.0f} MB")
        elif kind == 'error':
            error = msg.get('error')
            text = error.get('message', '') if isinstance(error, dict) else str(error)
            result.errors.append((msg.get('item', ''), text))
            self.log(f"  read error: {msg.get('item', '')}: {text}")
        elif kind == 'summary':
            result.summary = msg
            result.snapshot_id = msg.get('snapshot_id', '')
        elif kind == 'exit_error':
            self.log(f"  restic: {msg.get('message', '')}")
            return msg.get('message', '')
        return ''

    def forget(self, host, tag, retention):
        proc = self.run('forget', '--host', host, '--tag', tag, '--group-by', 'host,tags', *retention)
        for line in proc.stdout.splitlines():
            if line.startswith(('keep ', 'remove ')):   # "keep 9 snapshots:", "remove 1 snapshots:"
                self.log(f'  {line.strip()}')

    def ls(self, snapshot_id):
        """Every node in a snapshot: dicts with path, type and size."""
        nodes = []
        for line in self.run('ls', '--json', snapshot_id).stdout.splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if 'path' in obj and 'node' in (obj.get('message_type'), obj.get('struct_type')):
                nodes.append(obj)
        return nodes

    def dump(self, snapshot, path, out, host=None, tag=None):
        """Write one file from a snapshot to the open file out."""
        filters = (['--host', host] if host else []) + (['--tag', tag] if tag else [])
        proc = subprocess.run(['restic', 'dump', *filters, snapshot, path], env=self.env,
                              stdout=out, stderr=subprocess.PIPE, text=False)
        if proc.returncode != 0:
            raise ResticError('dump', proc.returncode, proc.stderr.decode(errors='replace'))
