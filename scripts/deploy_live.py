"""Stage and atomically deploy the plugin to a confirmed Legion Go 2 over SSH.

Requires paramiko and an already verified known_hosts entry. Passwords are read
from a prompt (or stdin with --password-stdin), never stored in project files.
"""
import argparse
import getpass
import hashlib
import json
import shlex
import sys
import time
import uuid
from pathlib import Path

import paramiko

REMOTE = r'''
import hashlib, json, os, re, signal, subprocess, sys, time, zipfile
from pathlib import Path
from deploy_migration import DeploymentMigration

archive, digest, stage_arg = sys.argv[1:]
stage = Path(stage_arg)
assert stage.parent == Path('/home/deck/.local/share/spotideck-dev')
assert re.fullmatch(r'deploy-[0-9TZ-]+-[a-f0-9]{8}', stage.name)
assert stage.resolve() == stage
assert Path(archive) == stage / 'plugin.zip' and not Path(archive).is_symlink()
assert Path('/sys/class/dmi/id/product_name').read_text().strip() == '83N0'
assert Path('/sys/class/dmi/id/product_version').read_text().strip() == 'Legion Go 8ASP2'
assert hashlib.sha256(Path(archive).read_bytes()).hexdigest() == digest
migration = DeploymentMigration(Path('/home/deck/homebrew'), stage)
migration.prepare()  # Detect mixed identities and unsafe paths before stopping Decky.
target = migration.target
unpacked = stage / 'unpacked'
unpacked.mkdir(mode=0o755)
with zipfile.ZipFile(archive) as z:
    for info in z.infolist():
        p = Path(info.filename)
        assert p.parts[0] == 'SpotiDeck' and not p.is_absolute() and '..' not in p.parts
        assert not info.is_dir() and (info.external_attr >> 16) & 0o170000 == 0o100000
        assert info.file_size < 20 * 1024 * 1024
    z.extractall(unpacked)
incoming = unpacked / 'SpotiDeck'
manifest = json.loads((incoming / 'plugin.json').read_text())
assert manifest['name'] == 'SpotiDeck' and manifest['flags'] == []
assert (incoming / 'main.py').is_file() and (incoming / 'dist/index.js').is_file()
for p in [incoming, *incoming.rglob('*')]:
    os.chown(p, 0, 0)
    p.chmod(0o755 if p.is_dir() else 0o644)

unit = 'plugin_loader.service'
cgroup = Path('/sys/fs/cgroup/system.slice/plugin_loader.service/cgroup.procs')
def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30).stdout.strip()
def pids():
    return [int(p) for p in cgroup.read_text().split()] if cgroup.exists() else []
def stop():
    run('systemctl', 'stop', unit)
    # This SteamOS unit uses KillMode=process. Drain only the unit's own cgroup.
    for sig, seconds in [(signal.SIGTERM, 8), (signal.SIGKILL, 3)]:
        for pid in pids():
            try: os.kill(pid, sig)
            except ProcessLookupError: pass
        end = time.monotonic() + seconds
        while pids() and time.monotonic() < end: time.sleep(0.1)
        if not pids(): break
    assert not pids(), 'Decky workers did not stop'
    props = run('systemctl', 'show', unit, '-p', 'MainPID', '-p', 'ControlPID')
    assert set(props.splitlines()) == {'MainPID=0', 'ControlPID=0'}, props

result = {'archive_sha256': digest, 'version': manifest['version'], 'stage': str(stage)}
stop_attempted = False
restart_allowed = True
try:
    stop_attempted = True
    stop()
    migration.install(incoming)
    before_start = time.time()
    run('systemctl', 'start', unit)
    assert run('systemctl', 'is-active', unit) == 'active'
    deadline = time.monotonic() + 25
    live = None
    while time.monotonic() < deadline:
        logs = Path('/home/deck/homebrew/logs/SpotiDeck')
        if logs.is_dir():
            for log in logs.glob('*.log'):
                if log.stat().st_mtime < before_start: continue
                matches = list(re.finditer(re.escape('SpotiDeck ' + manifest['version']) + r' loaded(?:[^\n]*)pid=(\d+)[^\n]*uid=(\d+)', log.read_text(errors='replace')))
                match = next((m for m in reversed(matches) if int(m[1]) in pids() and int(m[2]) == 1000), None)
                if match:
                    pid = int(match[1])
                    status = Path(f'/proc/{pid}/status').read_text()
                    assert int(re.search(r'^Uid:\s+\d+\s+(\d+)', status, re.M)[1]) == 1000
                    argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
                    # Verify only known fields; never output process arguments or player keys.
                    expected_title = ('SpotiDeck (' + str(target/'main.py') + ')').encode()
                    titled_worker = [arg for arg in argv if arg] == [expected_title]
                    named_worker = False
                    if b'--plugin-name' in argv:
                        name_index = argv.index(b'--plugin-name') + 1
                        named_worker = (name_index < len(argv) and argv[name_index] == b'SpotiDeck'
                                        and str(target/'main.py').encode() in argv)
                    assert titled_worker or named_worker, 'Worker has an unexpected main.py or Decky plugin name'
                    live = {'pid': pid, 'uid': 1000, 'log': str(log),
                            'executable': os.readlink(f'/proc/{pid}/exe'),
                            'main_path': str(target/'main.py'), 'plugin_name': 'SpotiDeck'}
                    break
        if live: break
        time.sleep(0.25)
    assert live, 'The installed SpotiDeck backend did not report a live non-root worker'
    result.update(status='installed', worker=live,
                  main_sha256=hashlib.sha256((target/'main.py').read_bytes()).hexdigest(),
                  bundle_sha256=hashlib.sha256((target/'dist/index.js').read_bytes()).hexdigest(),
                  **migration.summary())
except BaseException:
    result['status'] = 'rolled-back'
    if migration.changed:
        try:
            stop()
            migration.rollback()
        except BaseException as rollback_error:
            # Do not start Decky with a partially restored plugin/state layout.
            restart_allowed = False
            result['status'] = 'rollback-failed'
            result['rollback_error'] = str(rollback_error)
            raise
    raise
finally:
    try:
        if stop_attempted and restart_allowed: run('systemctl', 'start', unit)
    finally:
        (stage/'result.json').write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--password-stdin', action='store_true')
    args = parser.parse_args()
    password = sys.stdin.readline().rstrip('\r\n') if args.password_stdin else getpass.getpass('SSH/sudo password: ')
    root = Path(__file__).resolve().parents[1]
    version = json.loads((root / 'plugin.json').read_text())['version']
    archive = root / 'artifacts' / f'SpotiDeck-{version}.zip'
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    stage = '/home/deck/.local/share/spotideck-dev/deploy-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:8]
    c = paramiko.SSHClient()
    c.load_host_keys(str(Path.home() / '.ssh' / 'known_hosts'))
    c.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        c.connect(args.host, username='deck', password=password, timeout=8, allow_agent=False, look_for_keys=False)
        _, out, err = c.exec_command('mkdir -p -m 700 ' + shlex.quote(stage), timeout=10)
        if out.channel.recv_exit_status(): raise RuntimeError(err.read().decode())
        with c.open_sftp() as s:
            s.put(str(archive), stage + '/plugin.zip')
            with s.file(stage + '/deploy.py', 'w') as f: f.write(REMOTE)
            s.put(str(root/'scripts/deploy_migration.py'), stage + '/deploy_migration.py')
        command = 'sudo -S -p ' + shlex.quote('') + ' python3 ' + ' '.join(map(shlex.quote, [stage+'/deploy.py', stage+'/plugin.zip', digest, stage]))
        stdin, out, err = c.exec_command(command, timeout=90)
        stdin.write(password + '\n'); stdin.flush(); stdin.channel.shutdown_write()
        output, errors = out.read().decode(), err.read().decode()
        code = out.channel.recv_exit_status()
        (root/'artifacts/live').mkdir(parents=True, exist_ok=True)
        (root/'artifacts/live/deployment.txt').write_text(output + errors, encoding='utf-8')
        print(output, end='')
        if errors: print(errors, file=sys.stderr, end='')
        return code
    finally:
        c.close()


if __name__ == '__main__':
    raise SystemExit(main())
