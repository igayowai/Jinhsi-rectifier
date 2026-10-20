"""Build versioned portable EXE; Inno installer remains an explicit option."""
import argparse
import ast
from importlib import metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--format', choices=('portable', 'installer'))
    parser.add_argument('--iscc', type=Path)
    args = parser.parse_args()
    mode = args.format or ('installer' if args.iscc else 'portable')
    if sys.platform != 'win32':
        parser.error('Windows is required')
    if mode == 'installer' and (not args.iscc or not args.iscc.is_file()):
        parser.error('Installer requires an existing ISCC.exe')
    tree = ast.parse((PROJECT / 'rectifier/__init__.py').read_text(encoding='utf-8'))
    version = next(ast.literal_eval(node.value) for node in tree.body
                   if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets))
    output = PROJECT / 'releases' / version
    output.mkdir(parents=True, exist_ok=False)
    work = PROJECT / 'build' / version
    work.mkdir(parents=True, exist_ok=False)
    licenses = work / 'licenses'
    licenses.mkdir()
    for dist in metadata.distributions():
        for file in dist.files or ():
            if any(part.lower().startswith(('license', 'copying', 'notice')) for part in file.parts):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    destination = licenses / dist.metadata['Name'] / str(file)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if not python_license.is_file():
        raise FileNotFoundError('Python LICENSE.txt missing')
    shutil.copy2(python_license, licenses / 'Python-LICENSE.txt')
    (work / 'requirements.lock.txt').write_text(subprocess.check_output(
        [sys.executable, '-m', 'pip', 'freeze'], text=True), encoding='utf-8')
    digits = tuple(map(int, version.split('.'))) + (0,)
    resource = f"""VSVersionInfo(ffi=FixedFileInfo(filevers={digits!r}, prodvers={digits!r}, mask=0x3f, flags=0, OS=0x40004, fileType=0x1, subtype=0, date=(0,0)), kids=[StringFileInfo([StringTable('040904b0', [StringStruct('FileDescription', '묑류계 — 디시인사이드 정류기'), StringStruct('FileVersion', '{version}'), StringStruct('ProductName', '묑류계'), StringStruct('ProductVersion', '{version}')])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])])"""
    (work / 'version.txt').write_text(resource, encoding='utf-8')
    environment = os.environ.copy()
    environment['RECTIFIER_BUILD_ASSETS'] = str(work)
    with (work / 'pyinstaller.log').open('w', encoding='utf-8') as log:
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
            '--distpath', str(work / 'dist'), '--workpath', str(work / 'pyinstaller'),
            str(PROJECT / 'packaging' / ('portable.spec' if mode == 'portable' else 'rectifier.spec'))], cwd=PROJECT, env=environment,
            stdout=log, stderr=subprocess.STDOUT, check=True)
    if mode == 'portable':
        shutil.copy2(work / 'dist/묑류계.exe', output / f'묑류계-{version}.exe')
        print(output)
        return
    payload = work / 'dist/Rectifier'
    with (work / 'inno.log').open('w', encoding='utf-8') as log:
        subprocess.run([str(args.iscc.resolve()), f'/DAppVersion={version}',
            f'/DPayloadDir={payload}', f'/DOutputDir={output}',
            str(PROJECT / 'packaging/installer.iss')], cwd=PROJECT,
            stdout=log, stderr=subprocess.STDOUT, check=True)
    print(output)


if __name__ == '__main__':
    main()
