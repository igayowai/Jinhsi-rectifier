"""Offline inspection/extraction of a generated onefile EXE; no user secrets."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
from tempfile import TemporaryDirectory

import pefile
from PyInstaller.archive.readers import CArchiveReader

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_installed import smoke


def verify(exe, version):
    exe = Path(exe).resolve()
    archive = CArchiveReader(str(exe))
    names = {name.replace('\\', '/') for name in archive.toc}
    required = {'rectifier-worker.exe', 'python314.dll', '_tkinter.pyd',
                '_sqlite3.pyd', '_tcl_data/init.tcl', '_tk_data/tk.tcl',
                'rectifier/assets/app.ico', 'rectifier/assets/app.png', 'licenses/Python-LICENSE.txt',
                'sv_ttk/sv.tcl'}
    assert required <= names, sorted(required - names)
    for name in names:
        path = PurePosixPath(name.replace('\\', '/'))
        assert not path.is_absolute() and '..' not in path.parts
        assert path.suffix.lower() not in {'.sqlite', '.db', '.jsonl', '.env', '.log'}
        assert not {'trial-artifacts', 'diagnostics', 'shares', 'research'} & set(path.parts)
    needed_modules = {'rectifier.manager_ui', 'rectifier.bump_ui', 'rectifier.defense_runner', 'rectifier.recommend_action',
                      'rectifier.account_profiles', 'rectifier.branding', 'keyring.backends.Windows', 'sv_ttk'}
    modules = set(archive.open_embedded_archive('PYZ.pyz').toc)
    assert needed_modules <= modules
    pe = pefile.PE(str(exe))
    icons = next(e for e in pe.DIRECTORY_ENTRY_RESOURCE.entries if e.id == 3)
    icon_count = len(icons.directory.entries)
    assert icon_count == 26
    pe.close()
    with TemporaryDirectory(prefix='묑류계 실행형 검증 ') as tmp:
        root = Path(tmp)
        for name, entry in archive.toc.items():
            if entry[-1] in ('b', 'x', 'Z'):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extract(name))
        worker_modules = set(CArchiveReader(str(root / 'rectifier-worker.exe')).open_embedded_archive('PYZ.pyz').toc)
        assert needed_modules <= worker_modules
        smoke_result = smoke(root, version)
    return {'version': version, 'exe': exe.name, 'bytes': exe.stat().st_size,
            'sha256': hashlib.sha256(exe.read_bytes()).hexdigest(),
            'onefile_members': len(names), 'module_count': len(modules),
            'icon_frames': icon_count, 'worker_smoke': smoke_result,
            'user_data_included': False, 'fresh_pc_tested': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('exe', type=Path)
    parser.add_argument('version')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.exe, args.version)
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    (args.exe.parent / 'SHA256.txt').write_text(f"{report['sha256']}  {args.exe.name}\n", encoding='utf-8')
    print(json.dumps(report, indent=2))
