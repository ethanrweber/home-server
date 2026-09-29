"""A throwaway stack, repository and healthchecks server for the tests."""
import http.server
import json
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

BACKUP = Path(__file__).resolve().parents[1]
FAKES = BACKUP / 'tests' / 'fakes'


def make_db(path, rows=3, wal=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    if wal:
        con.execute('PRAGMA journal_mode=WAL')
    con.execute('CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)')
    con.executemany('INSERT INTO t (v) VALUES (?)', [(f'row {i}',) for i in range(rows)])
    con.commit()
    con.close()


def count(path):
    con = sqlite3.connect(path)
    try:
        return con.execute('SELECT count(*) FROM t').fetchone()[0]
    finally:
        con.close()


def write(path, text=''):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class Healthchecks:
    """Records every ping as (path, body)."""

    def __init__(self):
        pings = self.pings = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0)).decode()
                pings.append((self.path, body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, check):
        return f'http://127.0.0.1:{self.server.server_port}/{check}'

    def paths(self):
        return [p for p, _ in self.pings]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Sandbox:
    """A stack checkout, appdata, media library and /etc/restic, all under one temp dir."""

    def __init__(self, tmp, healthchecks):
        root = Path(tmp)
        self.repo, self.config, self.media = root / 'repo', root / 'config', root / 'media'
        self.etc, self.state, self.dumps = root / 'etc', root / 'state', root / 'dumps'
        self.fake, self.lib = root / 'fake', root / 'lib'
        for d in (self.repo, self.config, self.media, self.etc, self.fake, self.lib / 'sets'):
            d.mkdir(parents=True, exist_ok=True)

        # the stack: .env values that would break a shell that sourced them
        write(self.repo / '.env', f'CONFIG_ROOT={self.config}\nMEDIA_ROOT="{self.media}"\n'
                                  'WG_PRIVATE_KEY="a b $c #d"\nQBITTORRENT_PASS=p@ss;rm -rf /\n')
        write(self.repo / 'services/homepage/homepage.env', 'HOMEPAGE_VAR_X=1\n')
        make_db(self.config / 'App/Config/app.db')
        write(self.config / 'App/Config/config.xml', '<Config/>')
        write(self.config / 'App/Config/logs/app.txt', 'log')
        write(self.config / 'App/Backups/scheduled/app_backup.zip', 'zip')
        write(self.config / 'ts-app/state/tailscaled.state', '{"key": 1}')
        make_db(self.media / 'books/metadata.db')
        write(self.media / 'books/Author/Book (1)/book.epub', 'epub')

        # the sets, like backup/sets but pointing at this sandbox
        sets = self.lib / 'sets'
        write(sets / 'sets.conf', 'stack yes --keep-daily 7 --keep-weekly 4\n'
                                  'appzips no --keep-last 1\n'
                                  'media yes --keep-daily 7\n')
        write(sets / 'stack.paths', f'${{REPO_ROOT}}\n${{CONFIG_ROOT}}\n{self.etc}\n')
        write(sets / 'stack.canary', '${REPO_ROOT}/.env\n${CONFIG_ROOT}/ts-*/state/tailscaled.state\n')
        write(sets / 'appzips.paths', '${CONFIG_ROOT}/App/Backups\n')
        write(sets / 'media.paths', '${MEDIA_ROOT}/books\n')
        write(sets / 'stack.exclude', '# comment\n${CONFIG_ROOT}/**/logs\n${CONFIG_ROOT}/**/*.pre-restore-*\n')
        write(self.lib / 'volumes.allow', 'db:/var/lib/data   # a test volume\n')
        write(self.lib / 'read-errors.allow', '${CONFIG_ROOT}/App/Config/flaky*\n')

        write(self.etc / 'backup.conf', f'REPO_ROOT={self.repo}\nBACKUP_HOST=testhost\n')
        write(self.etc / 'test.pass', 'secret\n')
        write(self.etc / 'test.env', f'RESTIC_REPOSITORY=fake:repo\nRESTIC_PASSWORD_FILE={self.etc}/test.pass\n'
                                     'SETS="stack appzips media"\n'
                                     f'HC_BACKUP_URL={healthchecks.url("test-backup")}\n'
                                     f'HC_MAINT_URL={healthchecks.url("test-maint")}\n')
        # a second repository, which only hears about failures on this machine
        write(self.etc / 'other.env', f'HC_BACKUP_URL={healthchecks.url("other-backup")}\n')

    def env(self):
        env = dict(os.environ)
        env.update(RB_LIB=str(self.lib), RB_ETC=str(self.etc), RB_STATE=str(self.state),
                   RB_DUMPS=str(self.dumps), RB_CACHE=str(self.fake / 'cache'),
                   RB_LOCK=str(self.fake / 'lock'), RB_ALLOW_NONROOT='1',
                   FAKE_STATE=str(self.fake), PATH=f'{FAKES}:{env["PATH"]}',
                   RESTIC_PASSWORD='from the caller, must not leak')
        return env

    def run(self, *args, stdin=None):
        return subprocess.run([str(BACKUP / 'restic-backup'), *args], env=self.env(),
                              capture_output=True, text=True, input=stdin, timeout=120)

    def restore(self, *args, stdin=None):
        return subprocess.run([str(BACKUP / 'restore-db'), *args], env=self.env(),
                              capture_output=True, text=True, input=stdin, timeout=120)

    def scenario(self, **values):
        write(self.fake / 'scenario.json', json.dumps(values))

    def containers(self, *containers):
        write(self.fake / 'docker.json', json.dumps(list(containers)))

    def calls(self, command=None):
        path = self.fake / 'calls.jsonl'
        calls = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [c for c in calls if command is None or c['args'][0] == command]

    def docker_calls(self):
        path = self.fake / 'docker-calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def snapshot(self, tag):
        """The newest snapshot with this tag, as {path: node}."""
        snaps = [json.loads(p.read_text()) for p in sorted((self.fake / 'snapshots').glob('*.json'))]
        snaps = [s for s in snaps if tag in s['tags']]
        return {n['path']: n for n in snaps[-1]['nodes']} if snaps else None

    def age_stamp(self, repo, name, days):
        stamp = self.state / repo / name
        past = time.time() - days * 86400
        os.utime(stamp, (past, past))


def container(name, *mounts, running=True):
    """A container as docker inspect shows it. mounts: (type, source, destination, rw)."""
    return {'Id': f'id-{name}', 'Name': f'/{name}', 'State': {'Running': running},
            'Mounts': [{'Type': t, 'Source': s, 'Destination': d, 'RW': rw} for t, s, d, rw in mounts]}
