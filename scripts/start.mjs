#!/usr/bin/env node
// Shared setup and launcher for Windows, Linux and macOS.
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const WINDOWS = process.platform === 'win32';
const PYTHON = path.join(ROOT, '.venv', WINDOWS ? 'Scripts' : 'bin', WINDOWS ? 'python.exe' : 'python');

function run(command, args) {
  const result = spawnSync(command, args, { cwd: ROOT, stdio: 'inherit' });
  if (result.error) throw new Error(`Cannot run ${command}: ${result.error.message}`);
  if (result.status !== 0) process.exit(result.status ?? 1);
}

function usablePython(command, args = []) {
  const result = spawnSync(command, [...args, '-c',
    'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'],
  { cwd: ROOT, stdio: 'ignore' });
  return !result.error && result.status === 0;
}

function npm(command) {
  // npm is a .cmd file on Windows and must be run through cmd.exe.
  // These commands are fixed literals; no user arguments enter the shell.
  if (WINDOWS) run(process.env.ComSpec || 'cmd.exe', ['/d', '/s', '/c', `npm ${command}`]);
  else run('npm', command.split(' '));
}

function setup() {
  if (existsSync(path.join(ROOT, '.venv')) && !usablePython(PYTHON)) {
    throw new Error('The .venv is incompatible or damaged. Remove only .venv and retry; keep data and .env.');
  }
  if (!existsSync(PYTHON)) {
    const candidates = WINDOWS
      ? [['py', ['-3']], ['python', []], ['python3', []]]
      : [['python3', []], ['python', []]];
    const python = candidates.find(([command, args]) => usablePython(command, args));
    if (!python) throw new Error('Python 3.11 or later is required. Install it and add it to PATH.');
    run(python[0], [...python[1], '-m', 'venv', '.venv']);
  }
  run(PYTHON, ['-m', 'pip', 'install', '-r', 'requirements.txt']);
  npm('ci');
  npm('run build');
  console.log('Setup complete. Run npm start, start.bat (Windows), or ./start.sh (Linux/macOS).');
}

const args = process.argv.slice(2);
if (args.includes('--help') || args.includes('-h')) {
  console.log('Usage: npm start -- [--setup] [--no-open]\n  --setup    Install/update dependencies and build without starting\n  --no-open  Start without opening a browser');
} else {
  try {
    const unknown = args.find(arg => !['--setup', '--no-open'].includes(arg));
    if (unknown) throw new Error(`Unknown option: ${unknown}. Use --help for usage.`);
    const [major, minor] = process.versions.node.split('.').map(Number);
    if (major < 22 || (major === 22 && minor < 12)) {
      throw new Error('Node.js 22.12 or later is required.');
    }
    if (args.includes('--setup') || !existsSync(PYTHON) || !existsSync(path.join(ROOT, 'dist', 'index.html'))) {
      setup();
    }
    if (!args.includes('--setup')) {
      if (!usablePython(PYTHON)) throw new Error('The .venv is incompatible or damaged. Run setup again.');
      run(PYTHON, ['scripts/run.py', ...(args.includes('--no-open') ? [] : ['--open'])]);
    }
  } catch (error) {
    console.error(`Footprint: ${error.message}`);
    process.exitCode = 1;
  }
}
