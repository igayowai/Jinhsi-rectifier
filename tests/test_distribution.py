"""Offline checks for user-data location and the source CLI launch contract."""
from contextlib import closing
import json
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase

from rectifier import __version__, APP_NAME
from rectifier.paths import DATA_ROOT, PROGRAM_ROOT


def cli_command():
    return [sys.executable, '-X', 'utf8', '-B', str(PROGRAM_ROOT / 'rectifier_cli.py')]


class DistributionTests(TestCase):
    def test_user_data_path_is_outside_program_and_has_no_version(self):
        self.assertFalse(DATA_ROOT.is_relative_to(PROGRAM_ROOT))
        self.assertNotIn(__version__, DATA_ROOT.parts)

    def test_real_cli_from_unrelated_directory_and_version(self):
        with TemporaryDirectory() as tmp:
            result = subprocess.run([*cli_command(), '--version'], cwd=tmp,
                capture_output=True, encoding='utf-8', timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), f'{APP_NAME} {__version__}')
            demo = subprocess.run([*cli_command(), 'demo'], cwd=tmp,
                capture_output=True, encoding='utf-8', timeout=10)
            self.assertEqual(demo.returncode, 0, demo.stderr)
            self.assertEqual(json.loads(demo.stdout)['live_requests'], 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_removed_trial_commands_are_rejected(self):
        for command in ('trial', 'trial-reconcile'):
            result = subprocess.run([*cli_command(), command], capture_output=True, encoding='utf-8', timeout=10)
            self.assertEqual(result.returncode, 2)

    def test_migration_preserves_content_and_refuses_overwrite(self):
        helper = runpy.run_path(str(PROGRAM_ROOT / 'research/reports/migrate-distribution-data-20260916.py'))
        with TemporaryDirectory() as tmp:
            source, target = Path(tmp) / 'old', Path(tmp) / 'new'
            source.mkdir()
            with closing(sqlite3.connect(source / 'operations.sqlite')) as db:
                db.execute('CREATE TABLE operations (post_id INTEGER PRIMARY KEY, state TEXT)')
                db.execute("INSERT INTO operations VALUES (1, 'unknown')")
                db.commit()
            (source / 'archive.json').write_text('{"synthetic":true}')
            result = helper['migrate'](source, target)
            self.assertEqual(result['files'], 2)
            self.assertEqual(helper['database_contents'](source / 'operations.sqlite'),
                             helper['database_contents'](target / 'trial-artifacts/operations.sqlite'))
            self.assertEqual((source / 'archive.json').read_bytes(),
                             (target / 'trial-artifacts/archive.json').read_bytes())
            with self.assertRaises(FileExistsError):
                helper['migrate'](source, target)
            (source / 'unexpected.txt').write_text('synthetic')
            with self.assertRaises(ValueError):
                helper['migrate'](source, Path(tmp) / 'another')
            self.assertFalse((Path(tmp) / 'another').exists())
