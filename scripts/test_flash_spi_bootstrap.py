#!/usr/bin/env python3
"""Exercise full-package SPI verification/extraction offline (never invokes Fastboot)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from spi_bundle import CIX, CIX_MAGIC, DATA_OFFSET, PARTS, build_spi_bundle


SCRIPTS = Path(__file__).resolve().parent


def fixture(package, board, case):
    (package / 'images').mkdir(parents=True)
    (package / 'tools').mkdir()
    for name in ('Flash.cmd', 'Flash.sh', 'Flash-Contract.ps1', 'Flash-Debian.ps1', 'flash_contract.py'):
        destination = package if name in ('Flash.cmd', 'Flash.sh') else package / 'tools'
        shutil.copyfile(SCRIPTS / 'flash' / name, destination / name)
    for name in ('rootfs.img', 'gpt-512.img', 'gpt-4096.img', 'boot-512.img', 'boot-4096.img'):
        (package / 'images' / name).write_bytes(b'OS placeholder')
    legacy = case == 'legacy'
    if legacy:
        for name in ('ddr.bin', 'uefi.bin'):
            (package / 'images' / name).write_bytes(b'v1 placeholder')
        bl33 = None
    else:
        shutil.copyfile(SCRIPTS / 'spi_bundle.py', package / 'tools/spi_bundle.py')
        parts = []
        for name, kind, address, limit, flags in PARTS:
            size = limit if kind == 4 else 128
            data = CIX.pack(CIX_MAGIC, 1, 1, flags, kind, address, size, 0) + bytes([kind]) * size
            parts.append(data.ljust(4112, b'\0'))
        bl33 = parts[2]
        bundle = bytearray(build_spi_bundle(board, parts))
        if case == 'bad-trailer':
            bundle[-1] ^= 1
        elif case == 'bad-part-hash':
            bundle[DATA_OFFSET + 33] ^= 1
            bundle[-32:] = hashlib.sha256(bundle[:-32]).digest()
        elif case == 'bad-bl33-type':
            # Even with matching descriptor and outer hashes, the CIX type must be BL33.
            start = DATA_OFFSET + sum(map(len, parts[:2]))
            bundle[start + 16] = 3
            digest_offset = 48 + 2 * 36 + 4
            bundle[digest_offset:digest_offset + 32] = hashlib.sha256(bundle[start:-32]).digest()
            bundle[-32:] = hashlib.sha256(bundle[:-32]).digest()
        (package / 'images/myt-spi.bin').write_bytes(bundle)
        if case == 'duplicate-spi':
            (package / 'images/uefi.bin').write_bytes(bl33)
    inventory = {p.relative_to(package).as_posix(): {'bytes': p.stat().st_size,
                 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in package.rglob('*') if p.is_file()}
    hardware = ('Y12' if board == 'Y4' else 'Y4') if case == 'wrong-board' else board
    manifest = {'format': 'y4-flash-v1' if legacy else 'y4-flash-v2',
                'board_check': case != 'no-board-check', 'hardware': hardware + '-6187', 'files': inventory}
    encoded = json.dumps(manifest).encode()
    (package / 'tools/package.json').write_bytes(encoded)
    sums = {name: info['sha256'] for name, info in inventory.items()}
    sums['tools/package.json'] = hashlib.sha256(encoded).hexdigest()
    (package / 'SHA256SUMS').write_text(''.join(f'{digest}  {name}\n' for name, digest in sorted(sums.items())))
    return bl33


def check(command, cwd, success):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, errors='replace')
    assert (result.returncode == 0) == success, (command, result.stdout, result.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--powershell', help='also exercise this PowerShell executable (5.1 or 7)')
    args = parser.parse_args()
    hosts = ['python']
    if os.name == 'nt':
        hosts.append('cmd')
    else:
        hosts.append('shell')
    if args.powershell:
        hosts.append('powershell')
    cases = ('ok', 'legacy', 'bad-trailer', 'bad-part-hash', 'bad-bl33-type', 'wrong-board',
             'no-board-check', 'duplicate-spi', 'existing-output')
    for board in ('Y4', 'Y12'):
        for case in cases:
            with tempfile.TemporaryDirectory() as temporary:
                package = Path(temporary) / 'full'
                bl33 = fixture(package, board, case)
                for host in hosts:
                    output = Path(temporary) / (host + '.bin')
                    if case == 'existing-output':
                        output.write_bytes(b'keep')
                    if host == 'python':
                        command = [sys.executable, 'tools/flash_contract.py', 'package', '--extract-spi-bootstrap']
                    elif host == 'shell':
                        command = ['sh', 'Flash.sh', '--extract-spi-bootstrap']
                    elif host == 'cmd':
                        command = ['cmd.exe', '/c', 'Flash.cmd', '-ExtractSpiBootstrap']
                    else:
                        command = [args.powershell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                   'tools/Flash-Debian.ps1', '-ExtractSpiBootstrap']
                    check(command + [str(output)], package, case == 'ok')
                    if case == 'ok':
                        assert output.read_bytes() == bl33
                    elif case == 'existing-output':
                        assert output.read_bytes() == b'keep'
                    else:
                        assert not output.exists()
                assert not (package / 'logs').exists()
    print(f'PASS: Y4 and Y12, {len(cases)} cases each on {", ".join(hosts)}; no device access')


if __name__ == '__main__':
    main()
