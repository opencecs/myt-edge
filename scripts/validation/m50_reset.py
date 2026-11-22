#!/usr/bin/env python3
"""Run exactly one authorized M50 reboot and preserve its acceptance evidence.

No PCI configuration writes, rescans, slot power changes or recovery actions are
performed by this observer.  The sole hardware-changing action is reboot-device.
"""
import argparse
import datetime
import errno
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time

DEFAULT_CONTAINER = 'store-myt-compute-stack-inference-1'
DEFAULT_CLI = '/usr/local/houmo-sdk/tools/cli/hm_upgrade_cli'


def discover_m50(bdf=None, sys_pci=Path('/sys/bus/pci/devices')):
    if bdf is not None:
        bdf = bdf.lower()
        if not re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]', bdf):
            raise ValueError('Explicit BDF must have domain:bus:device.function form')
        paths = [sys_pci / bdf]
    else:
        paths = sorted(sys_pci.iterdir())
    matches = []
    for path in paths:
        if int((path / 'vendor').read_text().strip(), 16) == 0x1f6b and \
           int((path / 'device').read_text().strip(), 16) == 0x0c00:
            matches.append(path.name)
    if len(matches) != 1:
        raise RuntimeError(f'Expected exactly one M50 target, found {matches!r}')
    return matches[0]


def stamp():
    return {'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'monotonic': time.monotonic()}


def pcie_fields(data):
    if len(data) < 64 or data[:4] == b'\xff' * 4:
        raise ValueError('PCI config inaccessible or truncated')
    pos, seen = data[0x34] & 0xfc, set()
    while pos and pos not in seen:
        if pos < 0x40 or pos + 2 > len(data):
            raise ValueError('Invalid PCI capability list')
        seen.add(pos)
        if data[pos] == 0x10:
            if pos + 0x1c > len(data):
                raise ValueError('Truncated PCIe capability')
            ctl = struct.unpack_from('<H', data, pos + 8)[0]
            lcap = struct.unpack_from('<I', data, pos + 0x0c)[0]
            lctl, lsta = struct.unpack_from('<HH', data, pos + 0x10)
            sctl, ssta = struct.unpack_from('<HH', data, pos + 0x18)
            return {'cap_offset': pos, 'mps': 128 << ((ctl >> 5) & 7),
                    'mrrs': 128 << ((ctl >> 12) & 7),
                    'linkctl': lctl, 'linksta': lsta,
                    'slotctl': sctl, 'slotsta': ssta,
                    'dll_active_reporting': bool(lcap & (1 << 20)),
                    'dll_active': bool(lsta & 0x2000),
                    'link_training': bool(lsta & 0x800),
                    'link_width': (lsta & 0x3f0) >> 4,
                    'link_speed': lsta & 0xf}
        pos = data[pos + 1] & 0xfc
    raise ValueError('PCIe capability missing')


def read_config(path):
    fd = os.open(str(path / 'config'), os.O_RDONLY)
    try:
        return pcie_fields(os.pread(fd, 256, 0))
    finally:
        os.close(fd)


def tree_snapshot(domain):
    result = []
    for path in sorted(Path('/sys/bus/pci/devices').iterdir()):
        if not path.name.startswith(domain + ':'):
            continue
        item = {'bdf': path.name}
        try:
            item.update(vendor=(path / 'vendor').read_text().strip(),
                        device=(path / 'device').read_text().strip(),
                        driver=(path / 'driver').resolve().name
                        if (path / 'driver').exists() else None)
            item.update(read_config(path))
        except (OSError, ValueError) as exc:
            item['error'] = repr(exc)
        result.append(item)
    return result


def fixed_tree(tree, expected_bdfs=None, expected_count=10):
    return (len(tree) == expected_count
            and (expected_bdfs is None or {x['bdf'] for x in tree} == expected_bdfs)
            and all('error' not in x and x['mps'] == x['mrrs'] == 256
                    for x in tree))


def kmsg_messages(raw, since=0):
    messages = []
    for line in raw.splitlines():
        match = re.match(r'^\d+,(\d+),(\d+),[^;]*;(.*)$', line)
        if match and int(match[2]) / 1000000 >= since:
            messages.append({'seq': int(match[1]), 'time': int(match[2]) / 1000000,
                             'message': match[3]})
    return messages


def analyze_kmsg(messages):
    failures, corrected, unreadable = [], [], []
    rpmsg_up = False
    for entry in messages:
        msg = entry['message']
        if re.search(r'rpmsg device 0 link 0 up\b', msg):
            rpmsg_up = True
        if 'cannot read AER status' in msg:
            unreadable.append(entry)
        if re.search(r'CESta=00000001\b', msg):
            corrected.append(entry)
        bad = re.search(r'Kernel panic|\bSError\b|\bCall trace:|\bOops\b|\bBUG\b|'
                        r'Unable to handle kernel|'
                        r'PCIe fabric isolated|Invalid shmem physical address|'
                        r'IX8024 link wait .*expired|Failed to reset xh2a rpmsg link|'
                        r'xh2a.*(?:timed out|timeout|failed)|'
                        r'HDMA.*(?:timed out|timeout|failed)', msg, re.I)
        ue = re.search(r'\bUESta=([0-9a-fA-F]+)', msg)
        root = re.search(r'root err status=([0-9a-fA-F]+)', msg)
        if bad or (ue and int(ue[1], 16)) or (root and int(root[1], 16) & 0x7c):
            failures.append(entry)
    return {'rpmsg_up': rpmsg_up, 'failures': failures,
            'receiver_error_events': corrected,
            'aer_unreadable_during_transition': unreadable}


def run(args):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', args.run):
        raise ValueError('Run name must be one safe path component')
    root = Path(args.base)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = root / args.run
    output.mkdir(mode=0o700)  # Deliberately refuse to overwrite a prior run.
    status = {'run': args.run, 'start': stamp(), 'passed': False, 'complete': False,
              'reset_command_issued': False, 'scope': 'one reboot, no recovery'}
    stop, records, errors, threads = threading.Event(), [], [], []
    kmsgfd = portfd = None

    def save(name, value):
        (output / name).write_text(json.dumps(value, indent=2), encoding='utf-8')

    def capture(name, argv, timeout=20):
        result = {'argv': argv, 'start': stamp()}
        try:
            child = subprocess.run(argv, stdin=subprocess.DEVNULL,
                                   capture_output=True, timeout=timeout)
            result['rc'], stdout, stderr = child.returncode, child.stdout, child.stderr
        except subprocess.TimeoutExpired as exc:
            result['timeout'] = timeout
            stdout, stderr = exc.stdout or b'', exc.stderr or b''
        result['end'] = stamp()
        (output / (name + '.stdout')).write_bytes(stdout)
        (output / (name + '.stderr')).write_bytes(stderr)
        save(name + '.json', result)
        return result, stdout.decode(errors='replace')

    def reader():
        with (output / 'kmsg.log').open('wb', buffering=0) as stream:
            while True:
                try:
                    data = os.read(kmsgfd, 262144)
                    if data:
                        stream.write(data)
                        records.extend(kmsg_messages(data.decode(errors='replace')))
                    elif stop.is_set():
                        return
                    else:
                        stop.wait(.02)
                except BlockingIOError:
                    if stop.is_set():
                        return
                    stop.wait(.02)
                except OSError as exc:
                    errors.append({'source': 'kmsg', 'time': stamp(), 'error': repr(exc)})
                    if exc.errno != errno.EPIPE or stop.is_set():
                        return

    def sampler():
        previous = None
        with (output / 'port-samples.jsonl').open('w', buffering=1) as stream:
            while not stop.is_set():
                item = stamp()
                try:
                    lctl, lsta = struct.unpack('<HH', os.pread(portfd, 4, cap + 0x10))
                    sctl, ssta = struct.unpack('<HH', os.pread(portfd, 4, cap + 0x18))
                    if lsta == 0xffff or ssta == 0xffff:
                        raise ValueError('Upstream port inaccessible')
                    item.update(linkctl=lctl, linksta=lsta, slotctl=sctl, slotsta=ssta,
                                dll_active=bool(lsta & 0x2000),
                                slot_power_off=bool(sctl & 0x400),
                                endpoint_exists=endpoint.exists(),
                                driver_bound=(endpoint / 'driver').exists())
                    current = (lctl, lsta, sctl, ssta, item['endpoint_exists'], item['driver_bound'])
                    if current != previous:
                        print('PORT_CHANGE ' + json.dumps(item), flush=True)
                        previous = current
                except (OSError, ValueError, struct.error) as exc:
                    item['error'] = repr(exc)
                    errors.append({'source': 'port', **item})
                stream.write(json.dumps(item) + '\n')
                stop.wait(.05)

    try:
        save('result.json', status)  # Interrupted/panicked runs cannot look complete.
        bdf = discover_m50(args.bdf)
        endpoint = Path('/sys/bus/pci/devices') / bdf
        if (endpoint / 'vendor').read_text().strip() != '0x1f6b' or \
           (endpoint / 'device').read_text().strip() != '0x0c00':
            raise RuntimeError('Target BDF is not the expected M50')
        if not (endpoint / 'driver').exists():
            raise RuntimeError('M50 is not bound before reset')
        driver = (endpoint / 'driver').resolve().name
        port = endpoint.resolve().parent
        if (port / 'vendor').read_text().strip() != '0x1ec8' or \
           (port / 'device').read_text().strip() != '0x8024':
            raise RuntimeError('Target is not below IX8024')
        port_before = read_config(port)
        if not port_before['dll_active_reporting'] or not port_before['dll_active']:
            raise RuntimeError('IX8024 link must initially report active')
        cap = port_before['cap_offset']
        expected = tree_snapshot(bdf.split(':')[0])
        save('tree-before.json', expected)
        if not fixed_tree(expected, expected_count=args.expected_count):
            raise RuntimeError('Baseline domain tree is not entirely 256/256')
        bdfs = {item['bdf'] for item in expected}
        listing, text = capture('devices-before', ['docker', 'exec', args.container,
                                                 args.cli, 'list-devices'])
        identity = f'(ID: 0) - BDF: {bdf}'
        if listing.get('rc') != 0 or identity not in text:
            raise RuntimeError('CLI device ID 0 does not match target BDF')
        status.update(endpoint=bdf, requested_bdf=args.bdf, port=port.name, initial_driver=driver,
                      boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                      kernel=subprocess.check_output(['uname', '-a'], text=True).strip(),
                      cmdline=Path('/proc/cmdline').read_text().strip(),
                      release=json.loads(Path('/etc/y4-release.json').read_text()),
                      register_writes_by_observer=False)
        save('metadata.json', status)
        portfd = os.open(str(port / 'config'), os.O_RDONLY)
        kmsgfd = os.open('/dev/kmsg', os.O_RDONLY | os.O_NONBLOCK)
        os.lseek(kmsgfd, 0, os.SEEK_END)
        threads = [threading.Thread(target=reader), threading.Thread(target=sampler)]
        for thread in threads:
            thread.start()
        time.sleep(1)
        command_start = stamp()
        status['command_start'] = command_start
        status['reset_command_issued'] = True
        save('metadata.json', status)
        save('result.json', status)
        print('REBOOT_START ' + json.dumps(command_start), flush=True)
        command, text = capture('reboot-command', ['docker', 'exec', args.container,
                                                 args.cli, 'reboot-device', '-d', '0'], 35)
        status['command'] = command
        print('REBOOT_RESULT ' + json.dumps(command) + '\n' + text, flush=True)
        deadline = time.monotonic() + args.recovery_timeout
        recovered = None
        while time.monotonic() < deadline:
            try:
                current = tree_snapshot(bdf.split(':')[0])
                analyzed = analyze_kmsg([x for x in records if x['time'] >= command_start['monotonic']])
                same_driver = (endpoint / 'driver').exists() and (endpoint / 'driver').resolve().name == driver
                ready = same_driver and fixed_tree(current, bdfs, args.expected_count) and \
                    read_config(port)['dll_active'] and analyzed['rpmsg_up']
                if ready:
                    recovered = stamp()
                    break
            except (OSError, ValueError):
                pass
            time.sleep(.5)
        # Keep observing after initial recovery so a second drop cannot pass.
        if recovered:
            time.sleep(args.settle)
        status['recovered_at'] = recovered
        status['tree_after'] = tree_snapshot(bdf.split(':')[0])
        listing, text = capture('devices-after', ['docker', 'exec', args.container,
                                                args.cli, 'list-devices'])
        status['devices_after'] = {'command': listing, 'stdout': text}
        capture('port-after', ['lspci', '-D', '-s', port.name, '-vv'])
        capture('endpoint-after', ['lspci', '-D', '-s', endpoint.name, '-vv'])
        capture('dmesg-after', ['dmesg'])
        capture('container-logs', ['docker', 'logs', '--timestamps', '--since',
                                   command_start['utc'], args.container])
        capture('container-health', ['docker', 'inspect', '--format',
                                     '{{json .State.Health}}', args.container])
        stop.set()
        for thread in threads:
            thread.join(timeout=5)
        status['kmsg'] = analyze_kmsg([x for x in records if x['time'] >= command_start['monotonic']])
        sequences = [entry['seq'] for entry in records]
        status['kmsg_sequence_gaps'] = [
            {'previous': left, 'next': right}
            for left, right in zip(sequences, sequences[1:]) if right != left + 1
        ]
        identity_fields = ('bdf', 'vendor', 'device', 'driver')
        before_identity = {tuple(item.get(key) for key in identity_fields) for item in expected}
        after_identity = {tuple(item.get(key) for key in identity_fields) for item in status['tree_after']}
        port_after = read_config(port)
        status['port_after'] = port_after
        status['monitor_errors'] = errors
        status['checks'] = {
            'cli_returned_success': command.get('rc') == 0,
            'same_boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip() == status['boot_id'],
            'recovered_in_time': recovered is not None,
            'same_driver_bound': (endpoint / 'driver').exists() and (endpoint / 'driver').resolve().name == driver,
            'same_ten_functions_fixed256': fixed_tree(status['tree_after'], bdfs, args.expected_count),
            'same_tree_identity_and_drivers': before_identity == after_identity,
            'rpmsg_link_up_observed': status['kmsg']['rpmsg_up'],
            'cli_rediscovers_same_device': listing.get('rc') == 0 and identity in text,
            'no_new_fatal_shmem_or_timeout': not status['kmsg']['failures'],
            'monitor_complete': not errors and all(not t.is_alive() for t in threads),
            'kmsg_sequences_contiguous': not status['kmsg_sequence_gaps'],
            'upstream_link_active': port_after['dll_active_reporting'] and port_after['dll_active']
                                    and not port_after['link_training'],
            'upstream_link_speed_width_restored':
                port_after['link_speed'] == port_before['link_speed'] and
                port_after['link_width'] == port_before['link_width'],
            'slot_power_on': not bool(port_after['slotctl'] & 0x400),
        }
        status['passed'] = all(status['checks'].values())
    except Exception as exc:
        status['error'] = repr(exc)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=5)
        for descriptor in [portfd, kmsgfd]:
            if descriptor is not None:
                os.close(descriptor)
        status['end'] = stamp()
        status['complete'] = True
        status['monitor_errors'] = errors
        save('result.json', status)
        print('RESULT ' + json.dumps(status), flush=True)
    return 0 if status['passed'] else 1


def self_test():
    ce = kmsg_messages('6,7,1000000,-;Sky1 AER UESta=00000000 CESta=00000001\n')
    assert not analyze_kmsg(ce)['failures']
    assert len(analyze_kmsg(ce)['receiver_error_events']) == 1
    for message in ['Sky1 AER UESta=00040000 CESta=00000000',
                    'root err status=00000004 source=00000000',
                    'Invalid shmem physical address 0xffffffffffffffff',
                    'IX8024 link wait 5000 ms expired', 'Kernel panic - not syncing',
                    'SError Interrupt on CPU0', 'Call trace:', 'Internal error: Oops - SError',
                    'kernel BUG at drivers/pci/access.c:1!']:
        assert analyze_kmsg([{'message': message}])['failures'], message
    assert analyze_kmsg([{'message': 'rpmsg device 0 link 0 up'}])['rpmsg_up']
    assert not kmsg_messages('6,7,999999,-;rpmsg device 0 link 0 up', since=1)
    config = bytearray(256)
    config[0:4] = b'\x6b\x1f\x00\x0c'
    config[0x34] = 0x70
    config[0x70] = 0x10
    struct.pack_into('<H', config, 0x78, (1 << 5) | (1 << 12))
    struct.pack_into('<I', config, 0x7c, 0)
    struct.pack_into('<H', config, 0x82, 0x1044)
    fields = pcie_fields(config)
    assert fields['mps'] == fields['mrrs'] == 256
    assert not fields['dll_active_reporting'] and not fields['dll_active']
    tree = [{'bdf': str(i), 'mps': 256, 'mrrs': 256} for i in range(10)]
    assert fixed_tree(tree)
    assert not fixed_tree(tree[:-1])
    tree[-1]['mps'] = 128
    assert not fixed_tree(tree)
    with tempfile.TemporaryDirectory(prefix='m50-discovery-') as tmp:
        root = Path(tmp)
        for bdf, vendor, device in [('0003:d2:00.0', '0x1f6b', '0x0c00'),
                                    ('0001:30:00.0', '0x10ec', '0x8125')]:
            entry = root / bdf
            entry.mkdir()
            (entry / 'vendor').write_text(vendor)
            (entry / 'device').write_text(device)
        assert discover_m50(sys_pci=root) == '0003:d2:00.0'
        assert discover_m50('0003:D2:00.0', root) == '0003:d2:00.0'
        extra = root / '0000:d2:00.0'
        extra.mkdir()
        (extra / 'vendor').write_text('0x1f6b')
        (extra / 'device').write_text('0x0c00')
        try:
            discover_m50(sys_pci=root)
        except RuntimeError:
            pass
        else:
            raise AssertionError('Ambiguous automatic M50 selection accepted')
        assert discover_m50('0003:d2:00.0', root) == '0003:d2:00.0'
        for target in ['0001:30:00.0', '../not-a-bdf']:
            try:
                discover_m50(target, root)
            except (RuntimeError, ValueError):
                pass
            else:
                raise AssertionError('Invalid explicit M50 selection accepted')
        (extra / 'device').write_text('0xffff')
        (root / '0003:d2:00.0' / 'device').write_text('0xffff')
        try:
            discover_m50(sys_pci=root)
        except RuntimeError:
            pass
        else:
            raise AssertionError('Absent automatic M50 target accepted')
    print('SELF_TEST_PASS: CE allowance, fatal gates including SError/trace, timestamps, '
          'PCIe fields, full-tree gate, unique discovery and explicit BDF checks')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true', help='Pure offline tests; no device access')
    parser.add_argument('--run', help='Unique run name; existing directories are refused')
    parser.add_argument('--base', default='/data/m50-reboot-fix-20261113')
    parser.add_argument('--bdf', help='Optional explicit BDF; default discovers the unique 1f6b:0c00 M50')
    parser.add_argument('--container', default=DEFAULT_CONTAINER)
    parser.add_argument('--cli', default=DEFAULT_CLI)
    parser.add_argument('--expected-count', type=int, default=10)
    parser.add_argument('--recovery-timeout', type=float, default=60)
    parser.add_argument('--settle', type=float, default=5)
    arguments = parser.parse_args()
    if arguments.self_test:
        self_test()
    elif not arguments.run:
        parser.error('--run is required unless --self-test is used')
    else:
        sys.exit(run(arguments))
