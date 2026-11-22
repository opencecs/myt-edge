"""Build the common Y4/Y12 Debian userspace from a pinned package manifest."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

from apt_upgrade import install_manifest_packages
from rootfs_download import digest, download_all, fetch

HERE = Path(__file__).resolve().parent


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def configure_desktop(root):
    def write(name, text):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def link(name, target):
        path = root / name
        if path.exists() and not path.is_symlink():
            raise ValueError(f'unexpected file at {name}')
        path.unlink(missing_ok=True)
        path.symlink_to(target)

    gdm = root / 'etc/gdm3/daemon.conf'
    keys = ('XorgEnable', 'AutomaticLoginEnable', 'AutomaticLogin')
    lines = [line for line in gdm.read_text().splitlines() if line.partition('=')[0].strip() not in keys]
    offset = lines.index('[daemon]') + 1
    lines[offset:offset] = ['XorgEnable=false', 'AutomaticLoginEnable=true', 'AutomaticLogin=root']
    gdm.write_text('\n'.join(lines) + '\n')
    write('usr/share/glib-2.0/schemas/10-cix-settings-daemon.gschema.override',
          "[org.gnome.desktop.session]\nidle-delay=0\n[org.gnome.settings-daemon.plugins.power]\n"
          "sleep-inactive-ac-type='nothing'\n[org.gnome.shell]\nenabled-extensions=['unredirect@vaina.lt']\n"
          "disable-user-extensions=false\n[org.gnome.desktop.wm.keybindings]\nshow-desktop=['<Super>d']\n"
          "[org.gnome.software]\nallow-updates=false\n")
    pulse = root / 'etc/pulse/default.pa'
    content = pulse.read_text().rstrip()
    for index in range(1, 6):
        sink = f'load-module module-alsa-sink device=hw:0,{index}'
        if sink not in content.splitlines():
            content += '\n' + sink
    pulse.write_text(content + '\n')
    (root / 'etc/localtime').unlink(missing_ok=True)
    link('etc/localtime', '/usr/share/zoneinfo/Asia/Shanghai')
    shutil.copyfile(root / 'usr/share/applications/org.fcitx.Fcitx5.desktop',
                    root / 'etc/xdg/autostart/org.fcitx.Fcitx5.desktop')
    write('etc/apt/apt.conf.d/80donotinstallsuggested','APT::Install-Recommends "0";\nAPT::Install-Suggests "0";\n')
    write('etc/X11/default-display-manager','/usr/sbin/gdm3\n')
    link('etc/systemd/system/display-manager.service','/lib/systemd/system/gdm3.service')
    for unit in ('NetworkManager-wait-online.service','systemd-networkd-wait-online.service','swap.target'):
        link('etc/systemd/system/' + unit, '/dev/null')
    write('etc/default/cpufrequtils','GOVERNOR="performance"\n')
    write('etc/modules-load.d/raid.conf','raid0\nraid1\nraid10\nraid456\n')
    write('etc/systemd/system/getty@tty1.service.d/override.conf',
          '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin root --noclear %I 38400 linux\n')
    link('usr/bin/sh','/bin/bash')
    keyboard = root / 'etc/default/keyboard'
    lines = keyboard.read_text().splitlines()
    if sum(line.startswith('XKBLAYOUT=') for line in lines) != 1:
        raise ValueError('keyboard layout not found')
    keyboard.write_text('\n'.join('XKBLAYOUT="cn"' if line.startswith('XKBLAYOUT=') else line for line in lines)+'\n')
    interfaces = root / 'etc/network/interfaces'
    if 'iface lo inet loopback' not in interfaces.read_text():
        with interfaces.open('a') as stream:
            stream.write('\nauto lo\niface lo inet loopback\n')
    write('etc/default/locale','LANG=zh_CN.UTF-8\n')
    write('etc/profile.d/myt-desktop-locale.sh',
          'export LANG=zh_CN.UTF-8\nexport LC_ALL=zh_CN.UTF-8\nexport GTK_IM_MODULE=fcitx\n'
          'export QT_IM_MODULE=fcitx\nexport SAL_ENABLESKIA=1\n')
    write('etc/systemd/logind.conf.d/60-myt-power-key.conf','[Login]\nHandlePowerKey=ignore\n')


def install_packages(root, lock, cache, apt_sources):
    env = dict(os.environ, LC_ALL='C', DEBIAN_FRONTEND='noninteractive', SYSTEMD_OFFLINE='1', RUNLEVEL='1', INITRD='No')

    def guest(*args, **kwargs):
        return run('chroot',root,*args,env=env,**kwargs)

    install_manifest_packages(root, apt_sources)
    files = lock['common_packages']
    # Always use the verified local files, even when a mirror has moved on.
    archives = root / 'var/tmp/myt-debs'
    archives.mkdir()
    try:
        for name, entry in files.items():
            origin = cache / (entry['sha256'] + '.deb')
            package = archives / (name + '.deb')
            try:
                os.link(origin, package)
            except OSError:
                shutil.copyfile(origin, package)
        transform = lock['package_transforms']['gnome-core']
        with tempfile.TemporaryDirectory() as tmp:
            working = Path(tmp) / 'pkg'
            package = archives / 'gnome-core.deb'
            run('dpkg-deb','--raw-extract',package,working)
            control = working / 'DEBIAN/control'
            content = control.read_text()
            changes = [('Version: '+transform['source_version']+'\n','Version: '+transform['build_version']+'\n')]
            changes.extend((dep+', ','') for dep in transform['remove_depends'])
            for old,new in changes:
                if content.count(old) != 1:
                    raise ValueError('GNOME metapackage no longer matches the product transform')
                content = content.replace(old,new)
            control.write_text(content)
            package.unlink()  # The immutable source may also have other hard links.
            run('dpkg-deb','--root-owner-group','--build',working,package,
                env=dict(env,SOURCE_DATE_EPOCH=str(transform['source_date_epoch'])))
        opts = ['apt-get','-o','Dpkg::Use-Pty=0','-o','Dpkg::Options::=--force-confold',
                '-y','--no-install-recommends','--no-remove','--allow-downgrades']
        # APT must acquire local file URIs during installation. Hide remote sources
        # for this transaction so only the individually verified archives can be selected.
        guest(*opts,'-o','Dir::Etc::sourcelist=/dev/null','-o','Dir::Etc::sourceparts=-',
              'install',*('/var/tmp/myt-debs/'+name+'.deb' for name in sorted(files)))
    finally:
        shutil.rmtree(archives)
    for mark in ('manual','auto'):
        names = [name for name,e in files.items() if e['automatic'] == (mark=='auto')]
        guest('apt-mark',mark,*names)
    status = guest('dpkg-query','-W','-f='+'$'+'{Package}\t$'+'{db:Status-Status}\t$'+'{Version}\n',capture_output=True,text=True).stdout
    actual = {name:ver for name,state,ver in (line.split('\t') for line in status.splitlines()) if state=='installed'}
    expected = {name:e['installed_version'] for name,e in files.items()}
    if actual != expected:
        diff = {n:(actual.get(n),expected.get(n)) for n in actual.keys()|expected.keys() if actual.get(n)!=expected.get(n)}
        raise ValueError(f'installed Debian inventory differs from lock: {diff}')
    if guest('dpkg','--audit',capture_output=True,text=True).stdout.strip():
        raise ValueError('incomplete dpkg state')
    guest('apt-get','check')
    guest('debconf-communicate','gdm3',input='SET shared/default-x-display-manager gdm3\nFSET shared/default-x-display-manager seen true\n',text=True)
    configure_desktop(root)
    locales = {'en_US.UTF-8 UTF-8','es_ES.UTF-8 UTF-8','nb_NO.UTF-8 UTF-8','pl_PL.UTF-8 UTF-8','zh_CN.UTF-8 UTF-8'}
    p = root/'etc/locale.gen'
    lines = [line.lstrip('# ').strip() if line.lstrip('# ').strip() in locales else line for line in p.read_text().splitlines()]
    if not locales.issubset(lines):
        raise ValueError('required locales missing')
    p.write_text('\n'.join(lines)+'\n')
    guest('locale-gen')
    guest('apt-get','-o','Acquire::Retries=3','-o','Acquire::Languages=none','-o','APT::Update::Error-Mode=any','update')
    guest('appstreamcli','refresh-cache','--force')
    vendor = lock['vendor_files']
    members = vendor['cix_desktop_members']
    with tempfile.TemporaryDirectory() as tmp:
        run('dpkg-deb','-x',cache / (members['sha256']+'.deb'),tmp)
        for member in members['copy_verified_files_only']:
            source = Path(tmp) / member['source']
            if digest(source) != member['sha256']:
                raise ValueError(f'wrong CIX file {source.name}')
            dest = root / member['destination']
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            dest.chmod(0o644)
    override = vendor['hpl_owner_override']
    for path, mode in override['paths'].items():
        p = root / path.lstrip('/')
        if p.is_symlink() or stat.S_IMODE(p.stat().st_mode) != int(mode,8):
            raise ValueError(f'wrong HPL package type/mode: {path}')
        guest('dpkg-statoverride','--update','--add',override['owner'],override['group'],mode,path)
    print(f'Installed {len(files)} pinned packages into the common rootfs',flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root',type=Path)
    parser.add_argument('recipe',type=Path)
    parser.add_argument('cache',type=Path)
    parser.add_argument('--install-stage',action='store_true')
    args = parser.parse_args()
    root, recipe, cache = (p.resolve() for p in (args.root,args.recipe,args.cache))
    lock = json.loads(recipe.read_text())
    apt_sources = json.loads((recipe.parent/'inputs-apt.json').read_text())
    if lock['sources']['debian'] != apt_sources['sources'] or lock['sources']['docker'] != apt_sources['sources_extra']:
        raise ValueError('sources differ between the product selection and full package lock')
    for desired in apt_sources['packages']:
        selected = lock['common_packages'].get(desired['name'])
        if (selected is None or (desired.get('version') and desired['version'] != selected['installed_version'])
                or (desired.get('deb') and desired['deb']['sha256'] != selected['sha256'])):
            raise ValueError('refresh the package lock for ' + desired['name'])
    if args.install_stage:
        install_packages(root, lock, cache, apt_sources)
        return
    if not root.is_dir() or any(root.iterdir()) or root==Path('/'):
        raise ValueError('bootstrap requires an empty staging directory')
    for item in lock['vendor_files']['git']['files']:
        path = recipe.parent.parent / item['path']
        if path.is_symlink():
            actual = hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
        else:
            actual = digest(path)
        if actual != item['sha256']:
            raise ValueError('vendor input differs from lock: '+item['path'])
    download_all(lock, cache)
    key = lock['common_packages']['debian-archive-keyring']
    with tempfile.TemporaryDirectory() as tmp:
        run('dpkg-deb','-x',fetch(key,cache),tmp)
        keyring = Path(tmp)/'usr/share/keyrings/debian-archive-keyring.gpg'
        mirror = lock['sources']['debian'][0].split()[0]
        run('debootstrap','--arch='+lock['architecture'],'--variant=minbase','--merged-usr',
            '--force-check-gpg','--keyring='+str(keyring),lock['suite'],root,mirror)
    run('unshare','--mount','--propagation','private','bash',HERE/'rootfs_bootstrap_chroot.sh',root,
        sys.executable,'-B',Path(__file__).resolve(),root,recipe,cache,'--install-stage')
    (root/'usr/sbin/policy-rc.d').unlink(missing_ok=True)


if __name__=='__main__':
    main()
