"""End-to-end runs of restic-backup and restore-db against a fake restic and docker."""
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime

from helpers import BACKUP, Healthchecks, Sandbox, container, count, make_db, write


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.hc = Healthchecks()
        self.box = Sandbox(self.tmp, self.hc)

    def tearDown(self):
        self.hc.close()
        shutil.rmtree(self.tmp)

    def backup(self, *extra, expect=0):
        result = self.box.run('backup', 'test', *extra)
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result.stdout


class Backup(Base):
    def test_a_clean_run(self):
        out = self.backup()
        box = self.box
        stack = box.snapshot('stack')
        live = str(box.config / 'App/Config/app.db')
        copy = str(box.dumps / 'stack' / live.lstrip('/'))
        self.assertIn(copy, stack)                               # the consistent copy
        self.assertNotIn(live, stack)                            # not the live file
        self.assertIn(str(box.dumps / 'stack/manifest.json'), stack)
        self.assertIn(str(box.repo / '.env'), stack)
        self.assertIn(str(box.config / 'App/Config/config.xml'), stack)
        self.assertNotIn(str(box.config / 'App/Config/logs/app.txt'), stack)     # excluded
        self.assertNotIn(str(box.config / 'App/Backups/scheduled/app_backup.zip'), stack)
        self.assertIn(str(box.config / 'App/Backups/scheduled/app_backup.zip'), box.snapshot('appzips'))
        media = box.snapshot('media')
        self.assertIn(str(box.dumps / 'media' / str(box.media / 'books/metadata.db').lstrip('/')), media)
        self.assertNotIn(str(box.media / 'books/metadata.db'), media)

        self.assertEqual([c['args'][5] for c in box.calls('backup')], ['stack', 'appzips', 'media'])
        forgets = box.calls('forget')
        self.assertEqual(len(forgets), 3)
        self.assertIn('--group-by', forgets[0]['args'])
        self.assertEqual(forgets[1]['args'][-2:], ['--keep-last', '1'])
        for call in box.calls():
            self.assertEqual(call['password_file'], f'{box.etc}/test.pass')  # not the caller's RESTIC_PASSWORD
            self.assertEqual(call['cache'], str(box.fake / 'cache'))
        self.assertEqual(self.hc.paths(), ['/test-backup/start', '/test-backup'])
        self.assertIn('test: ok', out)

    def test_a_database_that_cannot_be_copied_is_backed_up_raw_and_fails_every_repository(self):
        bad = self.box.config / 'Broken/broken.db'
        make_db(bad, rows=500, wal=False)
        with open(bad, 'r+b') as f:
            f.seek(4096)
            f.write(os.urandom(8192))
        out = self.backup(expect=1)
        self.assertIn('could not copy database', out)
        self.assertIn(str(bad), self.box.snapshot('stack'))
        self.assertEqual(len(self.box.calls('backup')), 3)                     # the run carried on
        self.assertEqual(sorted(p for p in self.hc.paths() if p.endswith('/fail')),
                         ['/other-backup/fail', '/test-backup/fail'])

    def test_read_errors_fail_the_run_unless_allowed(self):
        c = self.box.config
        self.box.scenario(exit={'backup:stack': 3},
                          errors=[{'item': f'{c}/App/Config/flaky.tmp', 'message': 'no such file'}])
        self.assertIn('all on read-errors.allow', self.backup())
        forgets = len(self.box.calls('forget'))
        self.box.scenario(exit={'backup:stack': 3},
                          errors=[{'item': f'{c}/App/Config/config.xml', 'message': 'permission denied'}])
        out = self.backup(expect=1)
        self.assertIn('could not read 1 file', out)
        self.assertIn('/other-backup/fail', self.hc.paths())
        # the incomplete stack snapshot doesn't push complete ones out; the other sets forget as usual
        self.assertIn('kept all older stack snapshots', out)
        self.assertEqual([c['args'][4] for c in self.box.calls('forget')[forgets:]], ['appzips', 'media'])

    def test_exit_3_without_a_reported_file_is_still_a_failure(self):
        self.box.scenario(exit={'backup:media': 3})
        self.assertIn('media: restic could not read some files and did not say which', self.backup(expect=1))

    def test_the_snapshot_must_contain_every_copy_and_the_canary_files(self):
        self.box.scenario(drop=['manifest.json', 'tailscaled.state'])
        out = self.backup(expect=1)
        self.assertIn('missing the dump manifest', out)
        self.assertIn('ts-*/state/tailscaled.state', out)
        self.box.scenario(drop=['dumps/stack'])
        self.assertIn('missing the copy of', self.backup(expect=1))

    def test_a_repository_failure_only_fails_that_repository(self):
        self.box.scenario(exit={'backup:appzips': 1})
        out = self.backup(expect=1)
        self.assertIn('appzips: restic backup exited 1', out)
        self.assertEqual(len(self.box.calls('forget')), 2)
        self.assertIn('/test-backup/fail', self.hc.paths())
        self.assertNotIn('/other-backup/fail', self.hc.paths())

    def test_an_unreachable_repository_stops_the_run(self):
        self.box.scenario(exit={'unlock': 1})
        self.backup(expect=1)
        self.assertEqual(self.box.calls('backup'), [])
        self.assertEqual(self.hc.paths(), ['/test-backup/start', '/test-backup/fail'])

    def test_docker_volumes_must_be_allowed(self):
        self.box.containers(container('db', ('volume', '/var/lib/docker/volumes/x', '/var/lib/data', True)),
                            container('new', ('volume', '/var/lib/docker/volumes/y', '/photos', True)))
        out = self.backup(expect=1)
        self.assertIn('container new keeps /photos in a Docker volume', out)
        self.assertNotIn('container db', out)

    def test_recent_archives_and_database_servers_are_reported(self):
        write(self.box.config / 'NewApp/exports/export.zip', 'zip')
        write(self.box.config / 'Immich/postgres/PG_VERSION', '16')
        out = self.backup(expect=1)
        self.assertIn('NewApp/exports/export.zip is a recent archive', out)
        self.assertIn('looks like a Postgres data directory', out)

    def test_a_vanished_database_is_reported_once(self):
        extra = self.box.config / 'Old/old.db'
        make_db(extra)
        self.backup()
        shutil.rmtree(self.box.config / 'Old')
        self.assertIn(f'database {extra} was backed up last run', self.backup(expect=1))
        self.backup()

    def test_a_missing_path_fails_but_the_rest_is_backed_up(self):
        write(self.box.lib / 'sets/media.paths', '${MEDIA_ROOT}/books\n${MEDIA_ROOT}/music\n')
        out = self.backup(expect=1)
        self.assertIn('music does not exist', out)
        self.assertIn(str(self.box.media / 'books/Author/Book (1)/book.epub'), self.box.snapshot('media'))

    def test_a_malformed_healthchecks_url_is_logged_not_fatal(self):
        env = self.box.etc / 'test.env'
        env.write_text(env.read_text().replace('HC_BACKUP_URL=http://', 'HC_BACKUP_URL=nonsense://'))
        self.assertIn('healthchecks ping', self.backup())

    def test_a_config_error_fails_every_repository(self):
        (self.box.etc / 'test.pass').unlink()
        self.assertIn('RESTIC_PASSWORD_FILE', self.backup(expect=1))
        self.assertEqual(sorted(self.hc.paths()), ['/other-backup/fail', '/test-backup/fail'])

    def test_dry_run_changes_nothing(self):
        self.backup('--dry-run')
        self.assertTrue(all('--dry-run' in c['args'] for c in self.box.calls('backup')))
        self.assertEqual(self.box.calls('forget') + self.box.calls('unlock'), [])
        self.assertIsNone(self.box.snapshot('stack'))
        self.assertEqual(self.hc.pings, [])
        self.assertFalse((self.box.state / 'databases.json').exists())

    def test_show(self):
        result = self.box.run('show', 'test')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'{self.box.config}/**/logs', result.stdout)
        self.assertIn(f'{self.box.dumps}/stack', result.stdout)

    def test_refuses_to_run_without_root(self):
        env = self.box.env()
        del env['RB_ALLOW_NONROOT']
        if os.geteuid() == 0:
            self.skipTest('running as root')
        result = subprocess.run([str(BACKUP / 'restic-backup'), 'show', 'test'], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


class RestoreTest(Base):
    def setUp(self):
        super().setUp()
        tautulli = self.box.config / 'Tautulli/Config/tautulli.db'
        tautulli.parent.mkdir(parents=True)
        con = sqlite3.connect(tautulli)
        con.execute('CREATE TABLE session_history (id INTEGER PRIMARY KEY)')
        con.executemany('INSERT INTO session_history VALUES (?)', [(i,) for i in range(5)])
        con.commit()
        con.close()
        self.backup()

    def restore_test(self):
        env = self.box.env()
        env['RB_SCRATCH'] = self.tmp
        answers = 's3.us-east-005.backblazeb2.com\nbucket\nkeyid\nappkey\nsecret\n'
        # no controlling terminal, so the hidden prompts read stdin
        return subprocess.run([str(BACKUP / 'restore-test')], env=env, input=answers, text=True,
                              capture_output=True, start_new_session=True, timeout=120)

    def test_passes(self):
        result = self.restore_test()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Tautulli history: 5 plays restored, 5 live', result.stdout)
        self.assertIn('1 of 1 sidecar states', result.stdout)
        self.assertFalse(list(self.box.fake.parent.glob('restore-test-*')))   # secrets cleaned up
        restores = self.box.calls('restore')
        self.assertEqual(restores[0]['password_file'].rsplit('/', 1)[-1], 'pass')
        self.assertNotEqual(restores[0]['password_file'], f'{self.box.etc}/test.pass')

    def test_fails_when_a_secret_differs(self):
        write(self.box.repo / '.env', (self.box.repo / '.env').read_text() + 'NEW=1\n')
        result = self.restore_test()
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL .env is byte-identical', result.stdout)


class Maintenance(Base):
    def maint(self, expect=0):
        result = self.box.run('maint', 'test')
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return [c['args'] for c in self.box.calls() if c['args'][0] in ('prune', 'check')]

    def test_prune_and_check_when_due(self):
        month = datetime.now().month
        self.assertEqual(self.maint(), [['prune'], ['check', f'--read-data-subset={month}/12']])
        self.assertEqual(self.maint(), [['prune'], ['check', f'--read-data-subset={month}/12']])  # nothing new
        self.box.age_stamp('test', 'prune', 8)
        self.box.age_stamp('test', 'check', 8)
        self.assertEqual(self.maint()[2:], [['prune'], ['check']])
        self.assertEqual(self.hc.paths().count('/test-maint'), 3)

    def test_a_config_error_still_pings(self):
        (self.box.etc / 'backup.conf').unlink()
        self.maint(expect=1)
        self.assertEqual(self.hc.paths(), ['/test-maint/fail'])

    def test_a_failed_check_is_retried_next_time(self):
        self.box.scenario(exit={'check': 1})
        self.maint(expect=1)
        self.assertIn('/test-maint/fail', self.hc.paths())
        self.box.scenario()
        self.assertEqual(self.maint()[-1][0], 'check')


class Restore(Base):
    def setUp(self):
        super().setUp()
        self.live = self.box.config / 'App/Config/app.db'
        self.box.containers(
            container('app', ('bind', str(self.box.config / 'App/Config'), '/config', True)),
            container('dashboard', ('bind', str(self.box.config), '/everything', False)),   # read-only
            container('other', ('bind', str(self.box.config / 'Other'), '/config', True)))
        self.backup()
        make_db(self.live, rows=10)          # the app carries on after the backup
        write(f'{self.live}-wal', '')

    def test_restore_one_app(self):
        result = self.box.restore('app', '--repo', 'test', '--yes')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(count(self.live), 3)                     # as it was at the backup
        kept = sorted(p.name for p in self.live.parent.glob('app.db*.pre-restore-*'))
        self.assertEqual(sorted(k.split('.pre-restore-')[0] for k in kept), ['app.db', 'app.db-wal'])
        self.assertEqual(self.box.docker_calls(), [['stop', 'app'], ['start', 'app']])

    def test_the_set_aside_files_stay_out_of_later_backups(self):
        self.box.restore('app', '--repo', 'test', '--yes')
        self.backup()
        stack = self.box.snapshot('stack')
        self.assertFalse([p for p in stack if '.pre-restore-' in p])
        manifest = (self.box.dumps / 'stack/manifest.json').read_text()
        self.assertNotIn('pre-restore', manifest)

    def test_asks_first(self):
        result = self.box.restore('app', '--repo', 'test', stdin='no\n')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(count(self.live), 13)
        self.assertEqual(self.box.docker_calls(), [])

    def test_a_bad_copy_changes_nothing(self):
        snaps = sorted((self.box.fake / 'files').iterdir())
        for copy in snaps[0].rglob('app.db'):
            copy.write_bytes(b'SQLite format 3\x00' + os.urandom(8000))
        result = self.box.restore('app', '--repo', 'test', '--yes')
        self.assertEqual(result.returncode, 1)
        self.assertIn('stopped before changing anything', result.stderr)
        self.assertEqual(count(self.live), 13)
        self.assertEqual(self.box.docker_calls(), [])
        self.assertFalse(list(self.live.parent.glob('*.restore-tmp')))

    def test_rebuild_puts_every_database_back(self):
        self.box.containers()
        shutil.rmtree(self.box.config / 'App')
        result = self.box.restore('--set', 'stack', '--all', '--repo', 'test', '--yes')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(count(self.live), 3)
        self.assertEqual(os.stat(self.live).st_mode & 0o777, 0o644)

    def test_all_needs_a_set(self):
        result = self.box.restore('--all', '--repo', 'test', '--yes')
        self.assertEqual(result.returncode, 2)
        self.assertIn('--all needs --set', result.stderr)


if __name__ == '__main__':
    unittest.main()
