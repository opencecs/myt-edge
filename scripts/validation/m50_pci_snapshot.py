#!/usr/bin/env python3
"""Read-only Y4 fixed-domain PCI snapshot and comparison across two host boots.

Copy this file beside m50_reset.py. No reset, rescan, configuration register
write, sysfs write, or container command is executed. The output JSON is created
exclusively; an existing evidence file is never overwritten.
"""
import argparse
import copy
import datetime
import json
from pathlib import Path
import re
import struct
import sys
import tempfile

from m50_reset import read_config


EXPECTED_ROOTS = {
    'a010000': {'domain': '0000', 'root_bus': 'c0'},
    'a0e0000': {'domain': '0001', 'root_bus': '30'},
    'a070000': {'domain': '0002', 'root_bus': '90'},
    'a0d0000': {'domain': '0003', 'root_bus': '00'},
    'a0c0000': {'domain': '0004', 'root_bus': '60'},
}
BDF = re.compile(r'^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$')
ROOT_BUS = re.compile(r'^pci([0-9a-f]{4}):([0-9a-f]{2})$')
SLOT_ADDRESS = re.compile(r'^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}(?:\.[0-7])?$')


def path_location(path):
    parts = path.resolve().parts
    hosts = [part[:-5] for part in parts if part.endswith('.pcie')
             and part[:-5] in EXPECTED_ROOTS]
    buses = [ROOT_BUS.fullmatch(part) for part in parts if ROOT_BUS.fullmatch(part)]
    if len(hosts) != 1 or len(buses) != 1:
        raise ValueError(f'Expected one known host and one root bus in {path.resolve()}')
    ancestors = [part for part in parts[:-1] if BDF.fullmatch(part)]
    return hosts[0], {'domain': buses[0][1], 'root_bus': buses[0][2]}, \
        ancestors[-1] if ancestors else None


def collect(sys_pci=Path('/sys/bus/pci/devices'), sys_slots=Path('/sys/bus/pci/slots'),
            boot_id_path=Path('/proc/sys/kernel/random/boot_id')):
    data = {'schema': 1, 'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'boot_id': boot_id_path.read_text().strip(), 'roots': {}, 'devices': [],
            'slots': [], 'errors': [], 'register_writes': False}
    for path in sorted(sys_pci.iterdir()):
        item = {'bdf': path.name}
        try:
            if not BDF.fullmatch(path.name):
                raise ValueError('Unexpected PCI device directory name')
            host, root, parent = path_location(path)
            if host in data['roots'] and data['roots'][host] != root:
                raise ValueError('One host is associated with multiple root buses')
            data['roots'][host] = root
            item.update(host=host, parent=parent,
                        vendor=(path / 'vendor').read_text().strip().lower(),
                        device=(path / 'device').read_text().strip().lower(),
                        driver=(path / 'driver').resolve().name
                        if (path / 'driver').exists() else None)
            item.update(read_config(path))
        except (OSError, ValueError) as error:
            item['error'] = repr(error)
            data['errors'].append({'device': path.name, 'error': repr(error)})
        data['devices'].append(item)
    for slot in sorted(sys_slots.iterdir()):
        try:
            address = (slot / 'address').read_text().strip().lower()
            if not SLOT_ADDRESS.fullmatch(address):
                raise ValueError(f'Invalid slot address {address!r}')
            data['slots'].append({'name': slot.name, 'address': address})
        except (OSError, ValueError) as error:
            data['errors'].append({'slot': slot.name, 'error': repr(error)})
    return data


def identities(devices):
    keys = ('bdf', 'host', 'parent', 'vendor', 'device', 'driver')
    return sorted([tuple(item.get(key) for key in keys) for item in devices])


def assess(data, baseline=None):
    devices, roots = data['devices'], data['roots']
    fabric = [item for item in devices if item.get('host') == 'a010000']
    m50 = [item for item in devices if item.get('vendor') == '0x1f6b'
           and item.get('device') == '0x0c00']
    m50_bdf = m50[0]['bdf'] if len(m50) == 1 else None
    slots = sorted((slot['name'], slot['address']) for slot in data['slots'])
    root_bdfs = {value['domain'] + ':' + value['root_bus'] + ':00.0'
                 for value in EXPECTED_ROOTS.values()}
    checks = {
        'no_snapshot_errors': not data['errors'],
        'exact_fixed_host_domain_root_bus_mapping': roots == EXPECTED_ROOTS,
        'five_expected_root_functions_present': root_bdfs.issubset(
            {item['bdf'] for item in devices}),
        'device_domains_match_host_domains': all(
            item.get('host') in EXPECTED_ROOTS and
            item['bdf'].split(':')[0] == EXPECTED_ROOTS[item['host']]['domain']
            for item in devices),
        'ten_fabric_functions_fixed256': len(fabric) == 10 and all(
            item.get('mps') == item.get('mrrs') == 256 and 'error' not in item
            for item in fabric),
        'unique_m50_on_fabric_with_driver': len(m50) == 1 and
            m50[0].get('host') == 'a010000' and bool(m50[0].get('driver')),
        'm50_slot_address_present': m50_bdf is not None and any(
            address == m50_bdf or address == m50_bdf.rsplit('.', 1)[0]
            for _, address in slots),
        'unique_nonempty_slots': bool(slots) and len(slots) == len(set(slots)),
    }
    changes = {}
    if baseline is not None:
        old_result = assess(baseline)
        old_slots = sorted((slot['name'], slot['address']) for slot in baseline['slots'])
        old_ids, new_ids = identities(baseline['devices']), identities(devices)
        checks.update(
            baseline_itself_valid=old_result['passed'],
            different_nonempty_boot_ids=bool(data['boot_id']) and bool(baseline['boot_id'])
            and data['boot_id'] != baseline['boot_id'],
            all_pci_function_identities_stable=old_ids == new_ids,
            all_slot_names_and_addresses_stable=old_slots == slots,
        )
        changes = {'devices_removed_or_changed': [x for x in old_ids if x not in new_ids],
                   'devices_added_or_changed': [x for x in new_ids if x not in old_ids],
                   'slots_removed_or_changed': [x for x in old_slots if x not in slots],
                   'slots_added_or_changed': [x for x in slots if x not in old_slots]}
    return {'passed': all(checks.values()), 'checks': checks, 'changes': changes,
            'm50_bdf': m50_bdf, 'fabric_bdfs': sorted(item['bdf'] for item in fabric),
            'cross_boot_comparison_performed': baseline is not None}


def self_test():
    base = {'schema': 1, 'boot_id': 'boot-a', 'roots': copy.deepcopy(EXPECTED_ROOTS),
            'devices': [], 'slots': [{'name': '0', 'address': '0000:d2:00'}], 'errors': []}
    for host, root in EXPECTED_ROOTS.items():
        base['devices'].append({'bdf': root['domain'] + ':' + root['root_bus'] + ':00.0',
                               'host': host, 'parent': None, 'vendor': '0x1ec8',
                               'device': '0x8024', 'driver': 'pcieport', 'mps': 256,
                               'mrrs': 256})
    for bus in ['c1', 'c2', 'c3', 'c4', 'c5', 'c6', 'c7', 'c8', 'd2']:
        base['devices'].append({'bdf': '0000:' + bus + ':00.0', 'host': 'a010000',
                               'parent': '0000:c0:00.0', 'vendor': '0x1ec8',
                               'device': '0x8024', 'driver': 'pcieport', 'mps': 256,
                               'mrrs': 256})
    base['devices'][-1].update(vendor='0x1f6b', device='0x0c00', driver='xh2a_drv')
    assert assess(base)['passed']
    later = copy.deepcopy(base)
    later['boot_id'] = 'boot-b'
    assert assess(later, base)['passed']
    assert not assess(base, base)['passed']
    mutations = [
        lambda x: x['roots']['a010000'].update(domain='0002'),
        lambda x: x['roots']['a010000'].update(root_bus='00'),
        lambda x: x['devices'].pop(),
        lambda x: x['devices'][-1].update(mps=128),
        lambda x: x['devices'][-1].update(bdf='0002:d2:00.0'),
        lambda x: x['devices'][-1].update(driver=None),
        lambda x: x['slots'][0].update(name='0-1'),
        lambda x: x['slots'][0].update(address='0002:d2:00'),
        lambda x: x['errors'].append({'error': 'config read failed'}),
    ]
    for change in mutations:
        wrong = copy.deepcopy(later)
        change(wrong)
        assert not assess(wrong, base)['passed'], change
    with tempfile.TemporaryDirectory(prefix='m50-root-path-') as tmp:
        path = Path(tmp) / 'a010000.pcie' / 'pci0000:c0' / '0000:c0:00.0' / '0000:d2:00.0'
        assert path_location(path) == ('a010000', {'domain': '0000', 'root_bus': 'c0'},
                                       '0000:c0:00.0')
        try:
            path_location(Path(tmp) / 'unknown.pcie' / 'pci0000:c0' / '0000:d2:00.0')
        except ValueError:
            pass
        else:
            raise AssertionError('Unknown host accepted')
        fixture = Path(tmp) / 'fixture'
        pci, slots = fixture / 'pci', fixture / 'slots'
        pci.mkdir(parents=True)
        (slots / '0').mkdir(parents=True)
        (slots / '0' / 'address').write_text('0000:d2:00\n')
        boot = fixture / 'boot_id'
        boot.write_text('boot-fixture\n')
        for item in base['devices']:
            root = EXPECTED_ROOTS[item['host']]
            path = fixture / (item['host'] + '.pcie') / (
                'pci' + root['domain'] + ':' + root['root_bus'])
            if item['parent']:
                path /= item['parent']
            path /= item['bdf']
            path.mkdir(parents=True, exist_ok=True)
            (path / 'vendor').write_text(item['vendor'] + '\n')
            (path / 'device').write_text(item['device'] + '\n')
            driver = fixture / 'drivers' / item['driver']
            driver.mkdir(parents=True, exist_ok=True)
            (path / 'driver').symlink_to(driver)
            config = bytearray(256)
            config[:4] = b'\xc8\x1e\x24\x80'
            config[0x34] = 0x70
            config[0x70] = 0x10
            struct.pack_into('<H', config, 0x78, (1 << 5) | (1 << 12))
            (path / 'config').write_bytes(config)
            (pci / item['bdf']).symlink_to(path)
        observed = collect(pci, slots, boot)
        assert assess(observed)['passed'], observed
        assert identities(observed['devices']) == identities(base['devices'])
        (pci / '0000:d2:00.0' / 'config').write_bytes(b'')
        broken = collect(pci, slots, boot)
        assert broken['errors'] and not assess(broken)['passed']
    print('SELF_TEST_PASS: fixed mapping, 10-node 256 tree, slot/BDF identity, '
          'different-boot requirement, 9 failure controls, sysfs host/root resolution, '
          'full temporary sysfs collection and config-read failure')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--output', type=Path, help='New snapshot JSON, never overwritten')
    parser.add_argument('--baseline', type=Path, help='A valid snapshot from a different host boot')
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.output is None:
        parser.error('--output is required unless --self-test is used')
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    # Exclusive creation occurs before observing so duplicate evidence names fail early.
    with args.output.open('x') as output:
        try:
            data = collect()
            data['result'] = assess(data, baseline)
        except Exception as error:
            data = {'schema': 1, 'result': {'passed': False}, 'error': repr(error)}
        json.dump(data, output, indent=2)
        output.write('\n')
    print(json.dumps(data['result'], indent=2))
    return 0 if data['result']['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
