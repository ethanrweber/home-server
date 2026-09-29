"""Unit tests for the pieces: patterns, config files, sets, the scan and the database copies."""
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from resticbackup import config, dumps, scan, sets  # noqa: E402
from resticbackup.config import ConfigError  # noqa: E402
from resticbackup.patterns import Excludes, escape, glob_matches  # noqa: E402

from helpers import BACKUP, count, make_db, write  # noqa: E402


class Patterns(unittest.TestCase):
    """Same answers as restic 0.19.1's filter.Match for absolute patterns."""

    def match(self, pattern, path):
        return Excludes([pattern]).match(path)

    def test_a_pattern_covers_the_path_and_everything_under_it(self):
        self.assertTrue(self.match('/a/b', '/a/b'))
        self.assertTrue(self.match('/a/b', '/a/b/c/d'))
        self.assertFalse(self.match('/a/b', '/a/bc'))
        self.assertFalse(self.match('/a/b', '/a'))

    def test_double_star_is_any_number_of_directories_including_none(self):
        for path in ('/c/logs', '/c/Sonarr/logs', '/c/Sonarr/Config/logs/x.txt'):
            self.assertTrue(self.match('/c/**/logs', path), path)
        self.assertFalse(self.match('/c/**/logs', '/elsewhere/logs'))
        self.assertFalse(self.match('/c/**/logs', '/c/Sonarr/logsx'))
        self.assertTrue(self.match('/c/**/logs.db-*', '/c/Sonarr/logs.db-wal'))
        self.assertFalse(self.match('/c/**/logs.db-*', '/c/Sonarr/logs.db'))

    def test_globs_match_hidden_files(self):
        self.assertTrue(self.match('/c/graphs/.*.tmp', '/c/graphs/.funnel.tmp'))
        self.assertFalse(self.match('/c/graphs/.*.tmp', '/c/graphs/funnel.png'))

    def test_relative_patterns_are_refused(self):
        with self.assertRaises(ValueError):
            Excludes(['cache'])

    def test_escape_matches_only_the_literal_path(self):
        literal = '/m/Book [2019]/why*not?.db\\x'
        self.assertTrue(self.match(escape(literal), literal))
        self.assertTrue(self.match(escape(literal), literal + '/inside'))
        self.assertFalse(self.match(escape(literal), '/m/Book 2/whyXnot!.db\\x'))
        self.assertFalse(self.match(escape('/c/app.db'), '/c/app.db-wal'))

    def test_glob_matches_is_exact(self):
        self.assertTrue(glob_matches('/c/ts-*/state/x', '/c/ts-a/state/x'))
        self.assertFalse(glob_matches('/c/ts-*/state/x', '/c/ts-a/b/state/x'))
        self.assertFalse(glob_matches('/c/ts-*/state', '/c/ts-a/state/x'))


class ConfigFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_env_files_are_read_literally(self):
        path = write(self.tmp / 'x.env', '# comment\n\nexport A=1\nB="two words"\nC=\'$HOME\'\n'
                                         'D=has # hash\nE=a$b;c\n')
        self.assertEqual(config.parse_env_file(path),
                         {'A': '1', 'B': 'two words', 'C': '$HOME', 'D': 'has # hash', 'E': 'a$b;c'})

    def test_env_file_rejects_other_lines(self):
        path = write(self.tmp / 'x.env', 'not a setting\n')
        with self.assertRaises(ConfigError):
            config.parse_env_file(path)

    def test_stack_roots_come_from_dotenv_without_sourcing_it(self):
        (self.tmp / 'c').mkdir()
        (self.tmp / 'm').mkdir()
        write(self.tmp / '.env', f'WG="$(touch {self.tmp}/pwned)"\nCONFIG_ROOT={self.tmp}/c/  # appdata\n'
                                 f'MEDIA_ROOT="{self.tmp}/m"\n')
        self.assertEqual(config.read_stack_roots(self.tmp),
                         {'CONFIG_ROOT': f'{self.tmp}/c', 'MEDIA_ROOT': f'{self.tmp}/m'})
        self.assertFalse((self.tmp / 'pwned').exists())

    def test_stack_roots_must_be_existing_absolute_dirs(self):
        write(self.tmp / '.env', 'CONFIG_ROOT=relative\nMEDIA_ROOT=/nonexistent\n')
        with self.assertRaises(ConfigError):
            config.read_stack_roots(self.tmp)


class Sets(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ph = {'REPO_ROOT': '/r', 'CONFIG_ROOT': '/c', 'MEDIA_ROOT': '/m', 'DUMPS': '/d'}

    def load(self, files, local=None):
        for name, text in files.items():
            write(self.tmp / 'sets' / name, text)
        for name, text in (local or {}).items():
            write(self.tmp / 'etc' / name, text)
        return sets.load_sets(self.tmp / 'sets', self.tmp / 'etc', self.ph)

    def test_a_path_listed_by_another_set_is_excluded(self):
        defined = self.load({'sets.conf': 'stack yes --keep-last 3\nzips no --keep-last 1\n',
                             'stack.paths': '${CONFIG_ROOT}\n', 'zips.paths': '${CONFIG_ROOT}/A [x]/Backups\n'})
        self.assertIn(escape('/c/A [x]/Backups'), defined['stack'].excludes)
        self.assertEqual(defined['zips'].excludes, [])

    def test_dump_sets_get_their_dump_dir_and_local_paths(self):
        defined = self.load({'sets.conf': 'stack yes --keep-last 3\n', 'stack.paths': '${REPO_ROOT}\n'},
                            {'stack.local.paths': '/home/me/notes\n'})
        self.assertEqual(defined['stack'].paths, ['/r', '/home/me/notes', '/d/stack'])

    def test_bad_lists_are_refused(self):
        for files in ({'sets.conf': 'stack yes --keep-last 3\n', 'stack.paths': '${NOPE}/x\n'},
                      {'sets.conf': 'stack yes --keep-last 3\n', 'stack.paths': 'relative\n'},
                      {'sets.conf': 'stack yes --keep-last 3\n', 'stack.paths': '/r\n', 'stack.exclude': 'cache\n'},
                      {'sets.conf': 'stack maybe --keep-last 3\n', 'stack.paths': '/r\n'},
                      {'sets.conf': 'stack yes --prune\n', 'stack.paths': '/r\n'}):
            with self.subTest(files=files), self.assertRaises((ConfigError, ValueError)):
                self.load(files)

    def test_the_real_sets_parse(self):
        defined = sets.load_sets(BACKUP / 'sets', self.tmp / 'etc', self.ph)
        self.assertEqual(sorted(defined), ['appzips', 'comics', 'media', 'stack'])
        stack = defined['stack']
        for p in defined['appzips'].paths:
            self.assertIn(escape(p), stack.excludes)   # the app backup folders go in appzips only
        self.assertTrue(Excludes(stack.excludes).match('/c/Radarr/MediaCover/1/poster.jpg'))
        self.assertTrue(Excludes(stack.excludes).match('/c/Sonarr/logs.db-wal'))
        self.assertFalse(Excludes(stack.excludes).match('/c/Sonarr/sonarr.db'))
        self.assertFalse(Excludes(stack.excludes).match('/d/stack/c/Tautulli/cache/x.db'))
        self.assertTrue(Excludes(stack.excludes).match('/c/Calibre-Web-Automated/Config/processed_books/imported/x.zip'))


class Scan(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_scan_finds_databases_by_header_and_respects_excludes(self):
        c = self.tmp / 'c'
        make_db(c / 'App/data.bin')                  # a database whatever its name
        make_db(c / 'App/MediaCover/thumbs.db')      # excluded directory
        write(c / 'App/not-a.db', 'x' * 200)
        make_db(self.tmp / 'd/stack/copy.db')        # the dump dir is skipped
        os.symlink(c / 'App', c / 'link')             # symlinks aren't followed
        result = scan.scan([str(c), str(self.tmp / 'd/stack')], Excludes([f'{c}/**/MediaCover']),
                           config_root=str(c), skip=[str(self.tmp / 'd')])
        self.assertEqual(result.sqlite, [str(c / 'App/data.bin')])

    def test_scan_reports_database_servers_once_and_recent_archives(self):
        c = self.tmp / 'c'
        write(c / 'Immich/postgres/PG_VERSION', '16')
        write(c / 'Immich/postgres/base/1/PG_VERSION', '16')
        write(c / 'App/new.zip', 'zip')
        old = write(c / 'App/old.tar.gz', 'tgz')
        os.utime(old, (time.time() - 90 * 86400,) * 2)
        write(self.tmp / 'm/music/album.zip', 'zip')   # outside CONFIG_ROOT: not an app backup
        result = scan.scan([str(c), str(self.tmp / 'm')], Excludes([]), config_root=str(c))
        self.assertEqual(result.db_servers, [('Postgres', str(c / 'Immich/postgres'))])
        self.assertEqual(result.archives, [str(c / 'App/new.zip')])


class Dumps(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_copy_is_consistent_while_the_app_keeps_writing(self):
        src = self.tmp / 'live.db'
        make_db(src, rows=1000)
        stop = threading.Event()

        def writer():
            con = sqlite3.connect(src, timeout=30)
            while not stop.is_set():
                con.execute("INSERT INTO t (v) VALUES ('more')")
                con.commit()
            con.close()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            time.sleep(0.2)
            for i in range(5):
                dst = self.tmp / f'copy{i}.db'
                dumps.copy_database(str(src), str(dst))
                con = sqlite3.connect(dst)
                self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertGreaterEqual(count(dst), 1000)
                con.close()
        finally:
            stop.set()
            thread.join()

    def test_copy_keeps_the_original_mode_and_reads_a_cleanly_closed_wal_database(self):
        src = self.tmp / 'app.db'
        make_db(src)
        os.chmod(src, 0o640)
        self.assertFalse(Path(f'{src}-wal').exists())
        before = src.read_bytes()
        dumps.copy_database(str(src), str(self.tmp / 'copy.db'))
        self.assertEqual(os.stat(self.tmp / 'copy.db').st_mode & 0o777, 0o640)
        self.assertEqual(count(self.tmp / 'copy.db'), 3)
        self.assertEqual(src.read_bytes(), before)
        # like any reader, SQLite may create an empty -wal/-shm beside a cleanly closed WAL
        # database; they must belong to the database's owner (SQLite chowns them when root)
        for side in self.tmp.glob('app.db-*'):
            self.assertEqual(os.stat(side).st_uid, os.stat(src).st_uid)
            if side.name.endswith('-wal'):
                self.assertEqual(side.stat().st_size, 0)
        self.assertFalse(list(self.tmp.glob('copy.db*.tmp*')))

    def test_only_successful_copies_exclude_their_original(self):
        c = self.tmp / 'c'
        make_db(c / 'good.db')
        bad = c / 'bad.db'
        make_db(bad, rows=500, wal=False)
        with open(bad, 'r+b') as f:        # keep the header, wreck the pages
            f.seek(4096)
            f.write(os.urandom(8192))
        copied, excludes, problems = dumps.dump_all('stack', [str(c / 'good.db'), str(bad)],
                                                    self.tmp / 'd', {'CONFIG_ROOT': str(c)}, log=lambda *_: None)
        self.assertEqual([d.source for d in copied], [str(c / 'good.db')])
        self.assertEqual(len(problems), 1)
        self.assertIn('bad.db', problems[0])
        self.assertEqual(sorted(excludes), sorted(escape(f'{c}/good.db{s}') for s in ('', '-wal', '-shm', '-journal')))
        self.assertFalse(Excludes(excludes).match(str(bad)))
        manifest = (self.tmp / 'd/stack/manifest.json').read_text()
        self.assertIn('"relative": "good.db"', manifest)
        self.assertIn('"root": "CONFIG_ROOT"', manifest)
        self.assertTrue((self.tmp / 'd/stack' / str(c / 'good.db').lstrip('/')).exists())


if __name__ == '__main__':
    unittest.main()
