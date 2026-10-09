"""Exercise first-run setup in an isolated project, without network downloads."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def launcher(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for launcher integration checks')
    root = tmp_path / '足迹 project'
    scripts = root / 'scripts'
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2]
    shutil.copy(source / 'scripts/start.mjs', scripts / 'start.mjs')
    (root / 'requirements.txt').write_text('', encoding='utf-8')
    (scripts / 'run.py').write_text(
        'import json, pathlib, sys\n'
        'pathlib.Path("launched.json").write_text(json.dumps({"argv": sys.argv[1:], '
        '"python": sys.executable, "cwd": str(pathlib.Path.cwd())}), encoding="utf-8")\n',
        encoding='utf-8')
    commands = root / 'commands'
    commands.mkdir()
    (commands / 'fake-npm.cjs').write_text(
        'const fs = require("node:fs");\n'
        'const args = process.argv.slice(2);\n'
        'fs.appendFileSync("npm-calls.jsonl", JSON.stringify(args) + "\\n");\n'
        'if (args[0] === "run") {\n'
        '  if (process.env.FOOTPRINT_TEST_BUILD_FAIL) process.exit(7);\n'
        '  fs.mkdirSync("dist", {recursive: true});\n'
        '  fs.writeFileSync("dist/index.html", "built");\n'
        '}\n', encoding='utf-8')
    if os.name == 'nt':
        (commands / 'npm.cmd').write_text(
            f'@"{node}" "%~dp0fake-npm.cjs" %*\r\n', encoding='utf-8')
    else:
        for name, body in {
            'npm': f'exec {shlex.quote(node)} "$(dirname "$0")/fake-npm.cjs" "$@"',
            'python3': f'exec {shlex.quote(sys.executable)} "$@"',
        }.items():
            command = commands / name
            command.write_text(f'#!/bin/sh\n{body}\n', encoding='utf-8')
            command.chmod(0o755)
    env = {**os.environ, 'PATH': str(commands) + os.pathsep + os.environ['PATH'],
           'PIP_NO_INDEX': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1'}

    def invoke(*args, **extra_env):
        return subprocess.run([node, str(scripts / 'start.mjs'), *args],
                              cwd=tmp_path, env={**env, **extra_env},
                              capture_output=True, text=True, timeout=60)
    return root, invoke


def test_first_start_reuses_install_and_explicit_setup_rebuilds(launcher):
    root, invoke = launcher
    first = invoke('--no-open')
    assert first.returncode == 0, first.stdout + first.stderr
    launched = json.loads((root / 'launched.json').read_text(encoding='utf-8'))
    assert launched['argv'] == []
    assert Path(launched['cwd']) == root
    assert Path(launched['python']).parent.parent == root / '.venv'
    calls = root / 'npm-calls.jsonl'
    expected = [['ci'], ['run', 'build']]
    assert [json.loads(line) for line in calls.read_text().splitlines()] == expected

    second = invoke()
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads((root / 'launched.json').read_text())['argv'] == ['--open']
    assert [json.loads(line) for line in calls.read_text().splitlines()] == expected

    (root / 'launched.json').unlink()
    update = invoke('--setup')
    assert update.returncode == 0, update.stdout + update.stderr
    assert not (root / 'launched.json').exists()
    assert [json.loads(line) for line in calls.read_text().splitlines()] == expected * 2


def test_build_failure_does_not_start_server(launcher):
    root, invoke = launcher
    result = invoke('--no-open', FOOTPRINT_TEST_BUILD_FAIL='1')
    assert result.returncode == 7
    assert not (root / 'launched.json').exists()


def test_foreign_virtual_environment_is_not_overwritten(launcher):
    root, invoke = launcher
    (root / '.venv').mkdir()
    result = invoke('--no-open')
    assert result.returncode == 1
    assert 'incompatible or damaged' in result.stderr
    assert not (root / 'npm-calls.jsonl').exists()
