# Build via packaging/build.py; each EXE shares the collected runtime files.
from pathlib import Path
import os
from PyInstaller.utils.hooks import collect_data_files

project = Path(SPECPATH).parent
assets = Path(os.environ['RECTIFIER_BUILD_ASSETS'])
executables = []
analyses = []
for entry, name, console in [('rectifier_gui.py', 'Rectifier', False),
                              ('rectifier_cli.py', 'rectifier-worker', True)]:
    a = Analysis([str(project / entry)], pathex=[str(project)],
        binaries=[], datas=[(str(assets / 'licenses'), 'licenses'),
                            (str(project / 'rectifier/assets'), 'rectifier/assets'),
                            (str(project / 'packaging/사용법.txt'), '.'),
                            *collect_data_files('sv_ttk')],
        hiddenimports=['keyring.backends.Windows', 'sv_ttk'], hookspath=[],
        runtime_hooks=[], excludes=['pytest'], noarchive=False)
    pyz = PYZ(a.pure)
    executables.append(EXE(pyz, a.scripts, [], exclude_binaries=True,
        name=name, debug=False, bootloader_ignore_signals=False,
        strip=False, upx=False, console=console,
        icon=str(project / 'rectifier/assets/app.ico'),
        version=str(assets / 'version.txt'),
        manifest=str(project / 'packaging/rectifier.manifest')))
    analyses.append(a)
coll = COLLECT(*executables, *[a.binaries for a in analyses],
    *[a.datas for a in analyses], strip=False, upx=False, name='Rectifier')
