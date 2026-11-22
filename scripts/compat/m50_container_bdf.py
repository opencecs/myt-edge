#!/usr/bin/env python3
"""Source-hash-guarded compatibility patch for one deployed M50 image.

Only normalize_bdf() changes. No DB, permissions policy, hardware, container
configuration, or process lifecycle changes are performed by this utility.
"""
import argparse
import ast
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

ORIGINAL_SHA256 = 'c0230d5dd30d7638f670ba56f46beffea0dfbc4e90e33dd9d7ae66c743350c2c'
ORIGINAL_FUNCTION = '''def normalize_bdf(raw: str) -> str:
    s = (raw or "").strip().lower()
    if s.startswith("0002:") or BDF_RE.match(s):
        parts = s.split(":")
        if len(parts) == 3:
            bus, rest = parts[1], parts[2]
            dev, fn = rest.split(".")
            fn = str(int(fn, 10)) if fn.isdigit() else fn
            return f"{parts[0]}:{bus.zfill(2)}:{dev.zfill(2)}.{fn}"
    return s
'''
REPLACEMENT_FUNCTION = '''def normalize_bdf(raw: str) -> str:
    s = (raw or "").strip().lower()
    # PCI slot address files omit .function, in every domain. Keep that
    # omission so read_pci_topology can match all functions in the slot.
    match = re.fullmatch(
        r"(?:(?P<domain>[0-9a-f]{4}):)?(?P<bus>[0-9a-f]{1,2}):"
        r"(?P<dev>[0-9a-f]{1,2})(?:\\.(?P<fn>[0-7]))?", s)
    if not match:
        return s
    domain, bus, dev, fn = (match.group(key)
                            for key in ("domain", "bus", "dev", "fn"))
    prefix = f"{domain}:" if domain is not None else ""
    suffix = f".{fn}" if fn is not None else ""
    return f"{prefix}{bus.zfill(2)}:{dev.zfill(2)}{suffix}"
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def patched_bytes(original):
    if digest(original) != ORIGINAL_SHA256:
        raise ValueError('Source SHA256 is not the reviewed deployed version')
    old, new = ORIGINAL_FUNCTION.encode(), REPLACEMENT_FUNCTION.encode()
    if original.count(old) != 1:
        raise ValueError('Expected exactly one original normalize_bdf function')
    result = original.replace(old, new, 1)
    ast.parse(result)
    return result


def original_from_patched(data):
    old, new = ORIGINAL_FUNCTION.encode(), REPLACEMENT_FUNCTION.encode()
    if data.count(new) != 1:
        return None
    restored = data.replace(new, old, 1)
    return restored if digest(restored) == ORIGINAL_SHA256 else None


def load_actual_functions(data):
    wanted = {'normalize_bdf', 'read_pci_topology', 'choose_slot_key', '_domainless_key'}
    tree = ast.parse(data)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in wanted]
    if {node.name for node in functions} != wanted:
        raise ValueError('Real source functions needed for tests are missing')
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    unit = ast.fix_missing_locations(ast.Module(body=[future, *functions], type_ignores=[]))
    scope = {'re': re, 'Path': Path, 'SYS_PCI': Path('/not-used'),
             'SYS_SLOTS': Path('/not-used'),
             'BDF_RE': re.compile(r'^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.\d+$')}
    exec(compile(unit, 'actual-cards-functions.py', 'exec'), scope)
    return scope


def self_test(original):
    updated = patched_bytes(original)
    before, after = load_actual_functions(original), load_actual_functions(updated)
    cases = [
        ('0002:d2:00.0', '0002:d2:00.0'), ('0002:d2:00', '0002:d2:00'),
        ('0000:d2:00', '0000:d2:00'), ('0003:d2:00.7', '0003:d2:00.7'),
        (' 0002:D2:0.0 ', '0002:d2:00.0'), ('0003:a:1', '0003:0a:01'),
        ('d2:00.0', 'd2:00.0'), ('d2:00', 'd2:00'), ('a:1.7', '0a:01.7'),
        ('a:1', '0a:01'), ('', ''), (None, ''), ('0002:broken', '0002:broken'),
        ('0002:d2:00.0.1', '0002:d2:00.0.1'), ('0002:d2:00.9', '0002:d2:00.9'),
        ('0002:d2:gg', '0002:d2:gg'), ('0002:d2:', '0002:d2:'),
        ('0002:d2:00extra', '0002:d2:00extra'), ('junk', 'junk'),
    ]
    for raw, expected in cases:
        assert after['normalize_bdf'](raw) == expected, (raw, expected)
    try:
        before['normalize_bdf']('0002:d2:00')
    except ValueError:
        pass
    else:
        raise AssertionError('Original regression no longer reproduced')
    topology_cases = 0
    with tempfile.TemporaryDirectory(prefix='m50-bdf-test-') as tmp:
        base = Path(tmp)
        slots, pci = base / 'slots', base / 'pci'
        (slots / 'slot-1').mkdir(parents=True)
        pci.mkdir()
        for domain in ['0000', '0001', '0002', '0003', 'ffff']:
            for function in ['0', '7']:
                bdf = domain + ':d2:00.' + function
                (slots / 'slot-1' / 'address').write_text(domain + ':d2:00\n')
                value = after['read_pci_topology'](bdf, pci, slots)
                assert value['slot_name'] == 'slot-1', value
                topology_cases += 1
        (slots / 'slot-1' / 'address').write_text('0002:d2:00\n')
        try:
            before['read_pci_topology']('0002:d2:00.0', pci, slots)
        except ValueError:
            pass
        else:
            raise AssertionError('Original real read_pci_topology regression did not reproduce')
    assert original_from_patched(updated) == original
    try:
        patched_bytes(original + b'\n')
    except ValueError:
        pass
    else:
        raise AssertionError('Source hash guard accepted unknown bytes')
    with tempfile.TemporaryDirectory(prefix='m50-bdf-backup-test-') as tmp:
        base = Path(tmp)
        target = base / 'cards.py'
        alias = base / 'alias.py'
        alias.symlink_to(target)
        backup_cases = 0
        for data in (original, updated):
            target.write_bytes(data)
            for backup in (target, base / 'unused' / '..' / 'cards.py', alias):
                try:
                    apply(target, backup)
                except ValueError as error:
                    assert str(error) == 'Backup and target resolve to the same path'
                else:
                    raise AssertionError('Same resolved backup path accepted')
                assert target.read_bytes() == data
                backup_cases += 1
    return {'normalization_cases': len(cases), 'real_topology_cases': topology_cases,
            'same_resolved_backup_path_cases': backup_cases,
            'original_failure_reproduced': True, 'unknown_source_rejected': True,
            'idempotence_recognized': True, 'original_sha256': digest(original),
            'patched_sha256': digest(updated), 'passed': True}


def apply(path, backup):
    if path.resolve() == backup.resolve():
        raise ValueError('Backup and target resolve to the same path')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Target must be a regular file, not a symlink')
    original = path.read_bytes()
    prior = original_from_patched(original)
    if prior is not None:
        return {'status': 'already_applied', 'path': str(path), 'sha256': digest(original)}
    updated = patched_bytes(original)
    # Backup is exclusive. An existing backup must contain these exact bytes.
    try:
        fd = os.open(str(backup), os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                     stat.S_IMODE(info.st_mode))
    except FileExistsError:
        if backup.is_symlink() or backup.read_bytes() != original:
            raise ValueError('Existing backup is not the reviewed original')
    else:
        with os.fdopen(fd, 'wb') as output:
            output.write(original)
            output.flush()
            os.fsync(output.fileno())
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '.m50-bdf-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp, stat.S_IMODE(info.st_mode))
        os.chown(temp, info.st_uid, info.st_gid)
        if path.read_bytes() != original:
            raise ValueError('Target changed during patch preparation')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return {'status': 'applied', 'path': str(path), 'backup': str(backup),
            'original_sha256': digest(original), 'patched_sha256': digest(updated)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', type=Path, default=Path('/app/management/cards.py'))
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--diff', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if sum([args.self_test, args.diff, args.apply]) > 1:
        parser.error('Choose only one of --self-test, --diff or --apply')
    source = args.path.read_bytes()
    if args.apply:
        if args.backup is None:
            parser.error('--apply requires an explicit --backup path')
        print(json.dumps(apply(args.path, args.backup), indent=2))
    elif args.self_test:
        original = original_from_patched(source) or source
        print(json.dumps(self_test(original), indent=2))
    elif args.diff:
        original = original_from_patched(source) or source
        updated = patched_bytes(original)
        print(''.join(difflib.unified_diff(original.decode().splitlines(True),
                                         updated.decode().splitlines(True),
                                         'a/app/management/cards.py',
                                         'b/app/management/cards.py')), end='')
    else:
        original = original_from_patched(source) or source
        updated = patched_bytes(original)
        print(json.dumps({'status': 'already_applied' if source == updated else 'ready',
                          'original_sha256': digest(original),
                          'patched_sha256': digest(updated)}, indent=2))


if __name__ == '__main__':
    main()
