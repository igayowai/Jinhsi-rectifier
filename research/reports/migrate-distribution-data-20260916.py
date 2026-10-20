"""One-time, closed-app migration of public archives and moderation records.

Original files stay in place. An existing destination is never overwritten.
No credential store or browser state is accessed.
"""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

from platformdirs import user_data_path


def digest(path):
    with path.open('rb') as stream:
        return sha256(stream.read()).hexdigest()


def database_contents(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Database integrity check failed')
        schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        tables = [row[1] for row in schema if row[0] == 'table']
        content = [(table, sorted(map(repr, db.execute('SELECT * FROM "' + table.replace('"', '""') + '"'))))
                   for table in tables]
        return (db.execute('PRAGMA application_id').fetchone(),
                db.execute('PRAGMA user_version').fetchone(), schema, content)


def migrate(source, target):
    source, target = Path(source).resolve(), Path(target).absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError('Destination already exists; no overwrite allowed')
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError('Overlapping migration paths')
    items = list(source.rglob('*'))
    if source.is_symlink() or any(p.is_symlink() or p.is_junction() for p in items):
        raise ValueError('Linked source paths are not supported')
    files = [p for p in items if p.is_file()]
    if not files or any(p.suffix not in {'.json', '.png', '.sqlite'} for p in files):
        raise ValueError('Unexpected source file type; review before copying')
    before = {p.relative_to(source).as_posix(): digest(p) for p in files}
    original_db = source / 'operations.sqlite'
    contents = database_contents(original_db)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / ('.rectifier-migration-' + uuid4().hex)
    archive_root = staging / 'trial-artifacts'
    # Leave a failed staging copy for inspection. Never delete or replace source.
    shutil.copytree(source, archive_root)
    with closing(sqlite3.connect(original_db.as_uri() + '?mode=ro', uri=True)) as original:
        with closing(sqlite3.connect(archive_root / 'operations.sqlite')) as copied:
            original.backup(copied)
    after = {p.relative_to(source).as_posix(): digest(p) for p in source.rglob('*') if p.is_file()}
    if before != after:
        raise ValueError('Source changed during migration; close app and reconcile')
    for name, checksum in before.items():
        if name != 'operations.sqlite' and digest(archive_root / name) != checksum:
            raise ValueError('Copied file checksum mismatch')
    if database_contents(archive_root / 'operations.sqlite') != contents:
        raise ValueError('Copied database content mismatch')
    receipt = {'format': 'rectifier-data-migration-1', 'source': str(source),
               'files': len(before), 'source_sha256': before,
               'database_logically_equal': True, 'original_preserved': True}
    (staging / 'migration.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    # Windows rename fails if target appeared meanwhile. No replace/overwrite.
    staging.rename(target)
    return {'files': len(before), 'database_logically_equal': True, 'original_preserved': True}


if __name__ == '__main__':
    project = Path(__file__).resolve().parents[2]
    print(json.dumps(migrate(project / 'trial-artifacts',
          user_data_path('Rectifier', appauthor=False, version=None, roaming=False)), indent=2))
