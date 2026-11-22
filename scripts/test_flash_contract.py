#!/usr/bin/env python3
"""Run Linux flash flows against a fake Fastboot process, without hardware."""
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

MOCK = '''#!/usr/bin/env python3
import os,sys,json
from pathlib import Path
a=sys.argv[1:]
with Path(os.environ['MOCK_TRACE']).open('a') as f: f.write(' '.join(a)+'\\n')
mode=os.environ.get('MOCK_MODE','ok');sector=int(os.environ['MOCK_SECTOR']);cap=(32 if mode=='small-disk' else 64)*1024**3
boot=512*1024**2;slot=16*1024**3
statefile=Path(os.environ['MOCK_TRACE']+'.state')
state=json.loads(statefile.read_text()) if statefile.exists() else {'gpt':False}
if 'getvar' in a:
 name=a[a.index('getvar')+1]
 value={'serialno':'WRONG' if mode=='serial' else ('0xTEST-ONLY' if mode=='serial-prefix' else 'TEST-ONLY'),
        'debian-board': 'Y4' if mode=='board-mismatch' else 'Y12',
        'debian-flash-version': '0' if mode=='legacy' else ('1' if mode=='protocol1' else '3'),
        'debian-disk-info':f'block-size={sector},lastLBA={cap//sector-1},bytes={cap}',
        'debian-gpt-ready':'yes' if state['gpt'] and mode!='gpt-no' else 'no',
        'partition-size:boot':hex(boot),
        'partition-size:slot-a':hex(slot),
        'partition-size:slot-b':hex(slot),
        'partition-size:data':hex(((cap//sector-1-16384//sector)*sector//1048576)*1048576-10*1048576-boot-2*slot)}[name]
 if name.startswith('partition-size:') and mode=='stale-'+name.split(':')[1]: value=hex(int(value,16)-1048576)
 print('(bootloader) '+name+': '+value,file=sys.stderr)
elif 'flash' in a:
 part=a[a.index('flash')+1]
 assert part in ('gpt','boot','slot-a','slot-b')
 assert Path(a[a.index('flash')+2]).is_file()
 if part=='gpt':
  if mode=='gpt-failed0': print('FAILED (remote: GPT)',file=sys.stderr);sys.exit(0)
  state['gpt']=True
 else: assert state['gpt']
 if mode==part+'-failed0': print('FAILED (remote: write)',file=sys.stderr);sys.exit(0)
 print('OKAY',file=sys.stderr)
else: raise SystemExit(2)
statefile.write_text(json.dumps(state))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parent / 'flash'
    spec = importlib.util.spec_from_file_location('flash_contract', source / 'flash_contract.py')
    contract = importlib.util.module_from_spec(spec); spec.loader.exec_module(contract)
    cases = [(512, 'ok', 'auto', True, ['gpt', 'boot', 'slot-a', 'slot-b']),
             (4096, 'ok', 'auto', True, ['gpt', 'boot', 'slot-a', 'slot-b']),
             (512, 'serial-prefix', 'auto', True, ['gpt', 'boot', 'slot-a', 'slot-b']),
             (4096, 'ok', '512', False, []),
             (512, 'legacy', 'auto', False, []),
             (512, 'protocol1', 'auto', False, []),
             (512, 'serial', 'auto', False, []),
             (512, 'board-y12', 'auto', True, ['gpt', 'boot', 'slot-a', 'slot-b']),
             (512, 'board-mismatch', 'auto', False, []),
             (512, 'stale-boot', 'auto', False, ['gpt']),
             (512, 'stale-slot-a', 'auto', False, ['gpt']),
             (512, 'stale-slot-b', 'auto', False, ['gpt']),
             (512, 'stale-data', 'auto', False, ['gpt']),
             (512, 'small-disk', 'auto', False, []),
             (512, 'large-image', 'auto', False, []),
             (512, 'tamper', 'auto', False, []),
             (512, 'missing-hash', 'auto', False, []),
             (512, 'gpt-failed0', 'auto', False, ['gpt']),
             (512, 'gpt-no', 'auto', False, ['gpt']),
             (512, 'boot-failed0', 'auto', False, ['gpt', 'boot']),
             (512, 'slot-a-failed0', 'auto', False, ['gpt', 'boot', 'slot-a']),
             (512, 'slot-b-failed0', 'auto', False, ['gpt', 'boot', 'slot-a', 'slot-b'])]
    results = []
    for sector, mode, constraint, ok, expected_flashes in cases:
        out = args.output / (str(sector)+'-'+mode+'-'+constraint); out.mkdir()
        (out/'tools').mkdir(); (out/'images').mkdir()
        for name in ('Flash.cmd', 'Flash.sh', 'flash_contract.py', 'Flash-Debian.ps1', 'Flash-Contract.ps1'):
            target = out/name if name in ('Flash.cmd', 'Flash.sh') else out/'tools'/name
            shutil.copy2(source / name, target)
        count = 16385 if mode == 'large-image' else 64
        (out / 'images/rootfs.img').write_bytes(struct.pack('<IHHHHIIII', 0xed26ff3a, 1, 0, 28, 12, 1048576, count, 1, 0))
        for name in ('images/gpt-512.img', 'images/gpt-4096.img', 'images/boot-512.img', 'images/boot-4096.img', 'images/uefi.bin', 'images/ddr.bin'):
            (out / name).write_bytes(b'fixture-'+name.encode())
        files = {p.relative_to(out).as_posix():dict(bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
                 for p in sorted(out.rglob('*')) if p.is_file()}
        manifest = dict(format='y4-flash-v1',files=files)
        if mode.startswith('board-'):
            manifest['hardware'] = 'Y12-6187'
        (out/'tools/package.json').write_text(json.dumps(manifest))
        (out / 'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.relative_to(out).as_posix()+'\n'
                                               for p in sorted(out.rglob('*')) if p.is_file()))
        if mode == 'tamper': (out / 'images/boot-512.img').write_bytes(b'tampered')
        if mode == 'missing-hash':
            p=out/'SHA256SUMS';p.write_text('\n'.join(line for line in p.read_text().splitlines() if not line.endswith('  images/boot-512.img'))+'\n')
        binary = out / 'bin'; binary.mkdir(); fake = binary / 'fastboot'; fake.write_text(MOCK); fake.chmod(0o755)
        trace = out / 'trace.log'
        env = dict(os.environ, PATH=str(binary)+':'+os.environ['PATH'], MOCK_SECTOR=str(sector),
                   MOCK_MODE=mode, MOCK_TRACE=str(trace))
        result = subprocess.run(['sh', str(out/'Flash.sh'), 'TEST-ONLY', constraint],
                                capture_output=True, text=True, env=env)
        (out/'result.log').write_text(result.stdout+result.stderr)
        assert (result.returncode == 0) == ok, result.stdout+result.stderr
        calls = trace.read_text().splitlines() if trace.exists() else []
        flashes = [line.split(' flash ', 1)[1].split()[0] for line in calls if ' flash ' in line]
        assert flashes == expected_flashes, calls
        if mode.startswith('board-'):
            assert any(line.endswith('getvar debian-board') for line in calls), calls
        if mode == 'board-mismatch':
            assert not any(line.endswith('getvar debian-disk-info') for line in calls), calls
        if ok:
            gpt_index = next(i for i,line in enumerate(calls) if ' flash gpt ' in line)
            for name in ('serialno','debian-flash-version','debian-disk-info'):
                assert next(i for i,line in enumerate(calls) if line.endswith('getvar '+name)) < gpt_index
            gates = ('debian-gpt-ready', 'partition-size:boot', 'partition-size:slot-a',
                     'partition-size:slot-b', 'partition-size:data')
            for index, name in enumerate(gates, 1):
                assert calls[gpt_index+index].endswith('getvar '+name)
            assert not any(' erase ' in line or ' flash root ' in line or ' flash data ' in line for line in calls)
            expected_gpt = 'images/gpt-512.img' if sector == 512 else 'images/gpt-4096.img'
            assert any(line.endswith('flash gpt '+expected_gpt) for line in calls)
            expected_boot = 'images/boot-512.img' if sector == 512 else 'images/boot-4096.img'
            assert any(line.endswith('flash boot '+expected_boot) for line in calls)
        results.append(dict(sector=sector,mode=mode,exit=result.returncode,expected_success=ok,flashes=flashes))
    for malformed in ['block-size=512,lastLBA=1,bytes=1',
                      'block-size=512,lastLBA=18446744073709551615,bytes=0',
                      'block-size=4096,lastLBA=4503599627370495,bytes=18446744073709551616']:
        try:
            contract.geometry('debian-flash-version: 3', 'debian-disk-info: '+malformed,
                              args.output/'512-ok-auto/images/rootfs.img')
        except ValueError: pass
        else: raise AssertionError('Malformed disk geometry accepted')
    slow=args.output/'slow.py'
    slow.write_text('import time\nprint("INFO clearing",flush=True)\ntime.sleep(1.5)\nprint("OKAY",flush=True)\n')
    live=args.output/'live.log'
    start=time.monotonic()
    process=subprocess.Popen([sys.executable,str(source/'flash_contract.py'),'run','--log',str(live),'--',sys.executable,str(slow)],stdout=subprocess.PIPE,text=True)
    assert process.stdout.readline().strip()=='INFO clearing'
    assert process.poll() is None and time.monotonic()-start < 1.2
    assert 'INFO clearing' in live.read_text()
    assert process.communicate(timeout=5)[0].strip()=='OKAY' and process.returncode==0
    (args.output/'VERIFICATION.json').write_text(json.dumps(dict(complete=True,device_access=False,cases=results,live_output_verified=True),indent=2)+'\n')
    print('PASS: protocol3 A/B flash flow, 512/4Kn, serial/hash/layout checks, legacy rejection, FAILED rc0 stops, live output; no device access')


if __name__ == '__main__':
    main()
