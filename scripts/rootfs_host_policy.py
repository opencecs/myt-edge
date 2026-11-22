"""Apply and check the common always-on NAS policy in an isolated rootfs staging directory."""
from pathlib import Path
import ast
import configparser
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def target(root, name):
    p = root / name
    if Path(name).is_absolute() or not p.parent.resolve().is_relative_to(root):
        raise ValueError(name)
    return p

def absent(p):
    return not (p.is_symlink() or p.exists())

def apply(root, policy):
    system = root/'etc/systemd/system'
    tree_removals = {'usr/bin/MultiSys','usr/lib/MultiSys','userdata/aicp_test_aarch64',
                     'usr/share/gnome-shell/extensions/'+policy['removed_desktop_extension']}
    for name in policy['forbidden_host_files']:
        p = target(root,name)
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.is_dir() and name in tree_removals:
            shutil.rmtree(p)
        elif not absent(p):
            raise ValueError('unexpected file type '+name)
    for unit in ('cix_resume.service','cix_resume_prepare.service'):
        directory = system/(unit+'.d')
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    for name in policy['systemd_masks']:
        for pattern in ('*.wants','*.requires'):
            for directory in system.glob(pattern):
                p = target(root,str((directory/name).relative_to(root)))
                if p.is_symlink():
                    p.unlink()
                elif not absent(p):
                    raise ValueError('unexpected enablement '+str(p))
        p = target(root,'etc/systemd/system/'+name)
        if p.is_symlink() and os.readlink(p)=='/dev/null':
            continue
        if p.is_symlink():
            p.unlink()
        if p.exists():
            raise ValueError('unexpected regular unit override '+name)
        p.symlink_to('/dev/null')
    override = root/'usr/share/glib-2.0/schemas/10-cix-settings-daemon.gschema.override'
    before=override.read_text()
    key='enabled-extensions'
    matches=list(re.finditer(r'(?m)^enabled-extensions=(.+)$',before))
    if len(matches)!=1:
        raise ValueError('unexpected vendor GNOME preference')
    match=matches[0]
    old=ast.literal_eval(match[1])
    selected=[n for n in old if n!=policy['removed_desktop_extension']]
    if old!=selected:
        override.write_text(before[:match.start()]+key+'='+repr(selected)+before[match.end():])
    subprocess.run(['chroot',str(root),'glib-compile-schemas','--strict','/usr/share/glib-2.0/schemas'],check=True)

def validate(root, lock):
    policy=lock['host_policy']
    root=root.resolve()
    for name in policy['forbidden_host_files']:
        if not absent(target(root,name)):
            raise ValueError('container or hibernation payload present: '+name)
    for name in policy['systemd_masks']:
        if os.readlink(target(root,'etc/systemd/system/'+name))!='/dev/null':
            raise ValueError('unmasked '+name)
        for pattern in ('*.wants','*.requires'):
            if any(not absent(path/name) for path in (root/'etc/systemd/system').glob(pattern)):
                raise ValueError('unit is enabled: '+name)
    sleep=configparser.ConfigParser()
    sleep.read(root/'etc/systemd/sleep.conf.d/90-y4-always-on.conf')
    if any(sleep['Sleep'][k]!=v for k,v in policy['sleep_settings'].items()):
        raise ValueError('sleep policy mismatch')
    def command(*argv):
        return subprocess.check_output([str(s) for s in argv],text=True).strip()
    active=ast.literal_eval(command('chroot',root,'env','GSETTINGS_BACKEND=memory','gsettings','get','org.gnome.shell','enabled-extensions'))
    if policy['removed_desktop_extension'] in active or 'unredirect@vaina.lt' not in active:
        raise ValueError('compiled GNOME settings mismatch')
    kr=command('chroot',root,'dpkg-query','-W','-f=$'+'{Version}','systemd')
    release=json.loads((root/'etc/y4-release.json').read_text())['kernel']
    modules=list((root/'usr/lib/modules'/release).rglob('pcie-rkep.ko*'))
    if len(modules)!=1 or not modules[0].is_file(): raise ValueError('missing RK182X host module')
    mod=modules[0]
    ids=command('modinfo','-F','alias',mod).splitlines()
    if 'pci:v00001D87d0000182Asv*sd*bc*sc*i*' not in ids: raise ValueError('missing PCI alias')
    if command('modinfo','-F','name',mod)!='pcie_rkep': raise ValueError('module name')
    d=command('chroot',root,'modprobe','--set-version',release,'--show-depends','pcie-rkep')
    if 'pcie-rkep.ko' not in d: raise ValueError('module index')
    for asset in lock['vendor_files']['cix_desktop_members']['copy_verified_files_only']:
        if sha(target(root,asset['destination']))!=asset['sha256']: raise ValueError(asset['destination'])
    for name in ['usr/lib/firmware/dsp_fw.bin','usr/share/cix/lib/libnoe.so']:
        if not (root/name).is_file(): raise ValueError('missing SoC component '+name)
    if 'GOVERNOR="performance"' not in (root/'etc/default/cpufrequtils').read_text():
        raise ValueError('missing CPU governor')
    if not (root/'usr/share/dbus-1/system-services/net.hadess.PowerProfiles.service').read_text().count('SystemdService=power-profiles-daemon.service'):
        raise ValueError('power profile D-Bus activation no longer matches mask')
    return dict(systemd=kr,masked=policy['systemd_masks'],forbidden_paths_absent=len(policy['forbidden_host_files']),
                gnome_default_extensions=active,rk_module=str(mod.relative_to(root)),rk_module_sha256=sha(mod),pci_aliases=ids,
                runtime_graphics_assets=len(lock['vendor_files']['cix_desktop_members']['copy_verified_files_only']),kernel_release=release)
