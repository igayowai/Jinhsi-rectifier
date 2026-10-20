# PyInstaller owns extraction/cleanup. The worker shares that extracted runtime
# instead of unpacking a second onefile archive for every child operation.
from pathlib import Path
import os
from PyInstaller.utils.hooks import collect_data_files

project = Path(SPECPATH).parent
assets = Path(os.environ['RECTIFIER_BUILD_ASSETS'])
common = dict(debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    icon=str(project / 'rectifier/assets/app.ico'),
    version=str(assets / 'version.txt'),
    manifest=str(project / 'packaging/rectifier.manifest'))

def analyze(entry):
    return Analysis([str(project / entry)], pathex=[str(project)],
        binaries=[], datas=[(str(assets / 'licenses'), 'licenses'),
            (str(project / 'rectifier/assets'), 'rectifier/assets'),
            (str(project / 'packaging/사용법.txt'), '.'),
            *collect_data_files('sv_ttk')],
        hiddenimports=['keyring.backends.Windows', 'sv_ttk'], hookspath=[],
        runtime_hooks=[], excludes=['pytest'], noarchive=False)

worker_analysis = analyze('rectifier_cli.py')
worker = EXE(PYZ(worker_analysis.pure), worker_analysis.scripts, [],
    exclude_binaries=True, contents_directory='.', name='rectifier-worker',
    console=True, **common)
gui_analysis = analyze('rectifier_gui.py')
gui = EXE(PYZ(gui_analysis.pure), gui_analysis.scripts,
    gui_analysis.binaries, worker_analysis.binaries,
    gui_analysis.datas, worker_analysis.datas,
    [('rectifier-worker.exe', worker.name, 'DATA')],
    name='묑류계', console=False, **common)
