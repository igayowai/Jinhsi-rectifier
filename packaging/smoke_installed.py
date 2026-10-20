"""Offline installed-worker checks. Never authenticates or reads passwords."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rectifier import APP_NAME


def smoke(folder, version):
    folder = Path(folder).resolve()
    worker = folder / 'rectifier-worker.exe'
    with TemporaryDirectory() as tmp:
        result = subprocess.run([str(worker), '--version'], cwd=tmp,
            capture_output=True, encoding='utf-8', timeout=30)
        assert result.returncode == 0 and result.stdout.strip() == f'{APP_NAME} {version}', result.stderr
        demo = subprocess.run([str(worker), 'demo'], cwd=tmp,
            capture_output=True, encoding='utf-8', timeout=30)
        assert demo.returncode == 0, demo.stderr
        assert json.loads(demo.stdout)['live_requests'] == 0
        assert list(Path(tmp).iterdir()) == []
    return {'version': version, 'embedded_python_cli': True,
            'offline_demo': True, 'real_child_json_utf8': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    parser.add_argument('version')
    args = parser.parse_args()
    print(json.dumps(smoke(args.folder, args.version), indent=2))
