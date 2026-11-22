#!/usr/bin/env python3
"""Export minimal customer packages; retain build evidence outside the ZIPs."""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import time
import zipfile
from pathlib import Path

import parallel_zip
from spi_bundle import MAX_BUNDLE_SIZE, PARTS as BUNDLE_PARTS, read_spi_bundle

FORMAT = 'y4-flash-v2'
LEGACY_FORMAT = 'y4-flash-v1'
SYSTEM_PAYLOADS = {
    'rootfs_sparse.ext4': 'images/rootfs.img',
    'boot_sparse.img': 'images/boot-512.img',
    'boot_sparse-4096.img': 'images/boot-4096.img',
    'partition-table.img': 'images/gpt-512.img',
    'partition-table-4096.img': 'images/gpt-4096.img',
}
DDR_SOURCE = 'uefi/cix_memory_config_ota.bin'
BL33_SOURCE = 'uefi/cix_flash_ota.bin'
PAYLOADS = {**SYSTEM_PAYLOADS, DDR_SOURCE: 'images/ddr.bin', BL33_SOURCE: 'images/uefi.bin'}
HOST_FILES = {
    'Flash.cmd': 'Flash.cmd', 'Flash.sh': 'Flash.sh',
    'Flash-Debian.ps1': 'tools/Flash-Debian.ps1',
    'Flash-Contract.ps1': 'tools/Flash-Contract.ps1',
    'flash_contract.py': 'tools/flash_contract.py',
}
PM_IMAGE = 'images/pmcfg.bin'
BUNDLE_IMAGE = 'images/myt-spi.bin'
WINDOWS_TOOLS = ('fastboot.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll')
COMMON_FILES = (set(SYSTEM_PAYLOADS.values()) | set(HOST_FILES.values()) |
                {'tools/'+name for name in WINDOWS_TOOLS} | {'README.txt', 'SHA256SUMS', 'tools/package.json'})
SPI_TOOL = 'tools/spi_bundle.py'
FULL_FILES = COMMON_FILES | {BUNDLE_IMAGE, SPI_TOOL}
LEGACY_FULL_FILES = COMMON_FILES | {PAYLOADS[DDR_SOURCE], PM_IMAGE, PAYLOADS[BL33_SOURCE]}


def has_current_layouts(versions):
    return versions == [1, 2] and all(type(value) is int for value in versions)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(8*1024**2), b''):
            digest.update(data)
    return digest.hexdigest()


def item(path):
    return dict(bytes=path.stat().st_size, sha256=sha(path))


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True)+'\n', encoding='ascii')


def version_name(version):
    if not re.fullmatch(r'[0-9]{8}-r[0-9]+', version):
        raise ValueError('Invalid firmware version')
    return version


def copy_file(source, target, *, link=False):
    if source.is_symlink() or not source.is_file():
        raise ValueError('Expected a regular input file: '+str(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    # Build payloads are immutable after verification; hard links avoid
    # duplicating multi-gigabyte images in the delivery staging directory.
    if link:
        try:
            os.link(source, target)
            return
        except OSError:
            pass
    shutil.copy2(source, target)


def full_readme(version, board='Y4', *, bundle=False, layout_versions=None):
    project = '6187-R1P' if board == 'Y4' else 'Y12-6187'
    spi_step = f'''设备进入 Fastboot（已开机可执行 systemctl reboot bootloader），同一会话依次刷写：
   fastboot -s SERIAL flash bootloader images/ddr.bin
   fastboot -s SERIAL flash bootloader {PM_IMAGE}
   fastboot -s SERIAL flash bootloader images/uefi.bin
   fastboot -s SERIAL reboot'''
    if bundle and has_current_layouts(layout_versions):
        spi_step = f'''设备进入 Fastboot（已开机可执行 systemctl reboot bootloader）。确认设备序列号、实物板型为 {board}，然后查询：
   fastboot -s SERIAL getvar debian-board
   fastboot -s SERIAL getvar myt-spi-version
   fastboot -s SERIAL getvar myt-spi-layout
   板型为 {board}、myt-spi-version 为 1 且布局返回 ok,v1 或 ok,v2 开头，并列出 DDR/PM/UEFI 时，执行一次合并刷写：
   fastboot -s SERIAL flash myt-spi {BUNDLE_IMAGE}
   收到成功后执行 fastboot -s SERIAL reboot。
   若写入命令报 incompatible or unreadable onboard SPI table，说明未通过写前布局检查。保存串口日志，停止写入。

   旧接收端只需迁移一次。若能力查询明确返回不支持（例如 GetVar Variable Not found），先核对同一 USB 序列号、debian-board 和该机串口版本。首版接收端 f00b7cc14 会返回 myt-spi-version=1，但无法查询 myt-spi-layout。确认是这两类旧接收端之一后，在主机上从该板型的合并 BIN 提取已校验的单条目 BL33 到包外，再刷入：
   Windows：Flash.cmd -ExtractSpiBootstrap ../bootstrap-uefi.bin
   Linux：sh Flash.sh --extract-spi-bootstrap ../bootstrap-uefi.bin
   选择一个尚不存在的输出文件，提取成功后执行：
   fastboot -s SERIAL flash bootloader ../bootstrap-uefi.bin
   单写 BL33 成功后执行 fastboot -s SERIAL reboot，重新进入 Fastboot，再核对板型、能力和布局，执行合并刷写。
   若查询收到空响应、超时、板型不符或无法确认目标设备，请停止。myt-spi.bin 只写入 myt-spi 目标。
   如果只维护 SPI，到这里重启进入原系统即完成。仅在还要重装系统时执行第 3、4 步。'''
    elif bundle:
        spi_step = f'''本包包含首版合并格式的 {board} 板级固件，构建记录未确认接收端支持版本 2 的板载 NOR 表头。
   如需更新 SPI，请使用本板型同时支持表头版本 1 和 2 的新版 SPI 交付，按其说明操作。
   本包的 images/myt-spi.bin 含旧接收端，写入会重新安装该版本。
   如仅重装系统，可保留设备上的 SPI 固件，继续第 3 步。'''
    return f'''MYT {board} / {project} 完整刷机包 {version}

使用前备份系统盘，解压后按以下顺序操作。串口：57600 波特率。
Windows 使用包内 tools/fastboot.exe；Linux 使用系统 fastboot。
SERIAL 替换为 fastboot devices 显示的目标序列号。

1. 校验：Windows 执行 Flash.cmd -VerifyOnly；Linux 执行 sha256sum -c SHA256SUMS。
2. {spi_step}
   Windows 请把上述 fastboot 替换为 .\\tools\\fastboot.exe。
3. 重新进入 Fastboot，安装系统：
   Windows：Flash.cmd -SerialNumber SERIAL
   Linux：sh Flash.sh SERIAL auto
4. 成功后执行 fastboot -s SERIAL reboot，等待首次初始化完成。

脚本适配 512/4096 字节扇区，重写 GPT、boot 和两个系统槽。
data 保留要求原盘采用兼容布局和文件系统；迁移其他布局必须先备份。
DDR、CSU PM 配置及 UEFI（BL33）通过 USB Fastboot 更新。系统 OTA 仅升级系统和配套内核。原始全闪存镜像仅供构建校验。
此 UEFI 在未熔写设备上会自动执行不可逆 OEM eFuse 熔写并关机；须先取得设备所有者确认。
MCU 固件已随系统内置，由现有初始化流程处理，无需另刷文件。
本包版本以已编译固件为准；详细变更和验收记录见对应版本的发布说明。
'''


def ota_readme(version, board='Y4'):
    name = f'{board}-{version}.y4pkg'
    return f'''MYT {board} 系统 OTA 升级包 {version}

1. 解压并校验 SHA256SUMS，仅将 {name} 交给系统升级接口；不要上传 ZIP。
2. 将 .y4pkg 放到设备 /data 后执行：
   curl --fail-with-body --unix-socket /run/y4-board/api.sock -X POST -H 'Content-Type: application/octet-stream' -H 'Expect:' --upload-file /data/{name} http://localhost/api/v1/update
3. 收到 202 后等待自动写入、校验和重启，期间保持供电。

本包须匹配设备板型，版本须高于当前系统，且签名须由目标设备信任。
设备会将系统和配套内核写入非活动槽。重启后核对运行版本和已确认的启动槽。
板级 DDR、CSU PM 和 UEFI 通过同板型全量包中的合并 SPI BIN 单独更新。
详细变更和验收记录见对应版本的发布说明。
'''


def write_checksums(directory):
    files = {p.relative_to(directory).as_posix(): item(p)
             for p in sorted(directory.rglob('*')) if p.is_file() and p.name != 'SHA256SUMS'}
    (directory/'SHA256SUMS').write_text(''.join(value['sha256']+'  '+name+'\n'
                                              for name, value in files.items()), encoding='ascii')
    return dict(files, SHA256SUMS=item(directory/'SHA256SUMS'))


def verify_directory(directory, kind, version, board="Y4", *, bundle=False):
    directory = Path(directory)
    if kind == 'full':
        manifest = json.loads((directory/'tools/package.json').read_text())
        if manifest['format'] == FORMAT:
            if manifest.get('board_check') is not True:
                raise ValueError('Single-file SPI delivery must require a board check')
            expected = FULL_FILES
        elif manifest['format'] == LEGACY_FORMAT:
            expected = LEGACY_FULL_FILES | ({BUNDLE_IMAGE} if bundle else set())
        else:
            raise ValueError('Unsupported delivery manifest format')
    else:
        expected = {'README.txt', 'SHA256SUMS', f'{board}-{version_name(version)}.y4pkg'}
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob('*') if p.is_file()}
    if actual != expected or any(p.is_symlink() for p in directory.rglob('*')):
        raise ValueError('Unexpected or missing delivery files')
    sums = {}
    for line in (directory/'SHA256SUMS').read_text().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        if not match or match[2] not in expected or match[2] in sums:
            raise ValueError('Invalid delivery checksum inventory')
        sums[match[2]] = match[1]
    if set(sums) != expected-{'SHA256SUMS'}:
        raise ValueError('Incomplete delivery checksum inventory')
    files = {name: item(directory/name) for name in sorted(expected)}
    if any(files[name]['sha256'] != digest for name, digest in sums.items()):
        raise ValueError('Delivery checksum mismatch')
    if kind == 'full':
        if (manifest['version'] != version or manifest.get('hardware') != board+'-6187' or
                manifest['files'] != {n: v for n, v in files.items() if n not in ('tools/package.json', 'SHA256SUMS')}):
            raise ValueError('Delivery manifest mismatch')
        if BUNDLE_IMAGE in expected:
            parts = read_spi_bundle((directory/BUNDLE_IMAGE).read_bytes(), board)
            if manifest['format'] == LEGACY_FORMAT and parts != spi_parts(directory):
                raise ValueError('Combined SPI image differs from its verified components')
    return files


def verify_archive(archive, files):
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)) or set(names) != set(files):
            raise ValueError('Unexpected ZIP contents')
        for member in bundle.infolist():
            digest = hashlib.sha256()
            with bundle.open(member) as stream:
                for data in iter(lambda: stream.read(8*1024**2), b''):
                    digest.update(data)
            if dict(bytes=member.file_size, sha256=digest.hexdigest()) != files[member.filename]:
                raise ValueError('ZIP payload mismatch: '+member.filename)


def archive_delivery(directory, output, kind, version, jobs, large_entry, board="Y4"):
    archive = output/f'{board}-{version}-{kind}.zip'
    start = time.monotonic()
    compression = parallel_zip.create(directory, archive, jobs, output/(kind+'-zip.log'), large_entry, level=6 if kind == 'ota' else 1)
    seconds = time.monotonic()-start
    start = time.monotonic()
    metadata = item(archive)
    return dict(path=str(archive.resolve()), seconds=seconds, sha256_seconds=time.monotonic()-start,
                compression=compression, **metadata)


def spi_parts(directory):
    return [(directory/'images'/name).read_bytes() for name, *_ in BUNDLE_PARTS]


def create_full(build_package, host_source, output, jobs=8, *, spi_bin):
    build_package, host_source, output, spi_bin = map(Path, (build_package, host_source, output, spi_bin))
    release = json.loads((build_package/'y4-release.json').read_text())
    manifest = json.loads((build_package/'MANIFEST.json').read_text())
    board = release.get('board', 'Y4')
    if board not in ('Y4', 'Y12') or manifest.get('hardware') != f'{board}-6187':
        raise ValueError('Full package release and manifest board differ')
    version = version_name(release['rootfs'])
    if spi_bin.is_symlink() or not spi_bin.is_file() or spi_bin.stat().st_size > MAX_BUNDLE_SIZE:
        raise ValueError('Expected one regular, board-specific SPI BIN')
    data = spi_bin.read_bytes()
    read_spi_bundle(data, board)
    required = set(SYSTEM_PAYLOADS) | {'tools/'+name for name in WINDOWS_TOOLS} | {'y4-release.json'}
    for name in required:
        if item(build_package/name) != manifest['files'][name]:
            raise ValueError('Build payload checksum mismatch: '+name)
    directory = output/'full'
    directory.mkdir(parents=True, exist_ok=False)
    for name, target in SYSTEM_PAYLOADS.items():
        copy_file(build_package/name, directory/target, link=True)
    (directory/BUNDLE_IMAGE).write_bytes(data)
    copy_file(Path(__file__).with_name('spi_bundle.py'), directory/SPI_TOOL)
    for name, target in HOST_FILES.items():
        copy_file(host_source/name, directory/target)
    for name in WINDOWS_TOOLS:
        copy_file(build_package/'tools'/name, directory/'tools'/name)
    (directory/'README.txt').write_text(full_readme(version, board, bundle=True,
                                                   layout_versions=[1, 2]), encoding='utf-8')
    metadata = dict(format=FORMAT, version=version, hardware=manifest['hardware'],
                    board_check=True,
                    files={p.relative_to(directory).as_posix(): item(p)
                           for p in sorted(directory.rglob('*')) if p.is_file()})
    write_json(directory/'tools/package.json', metadata)
    write_checksums(directory)
    files = verify_directory(directory, 'full', version, board, bundle=True)
    archive = archive_delivery(directory, output, 'full', version, jobs, 'images/rootfs.img', board)
    result = dict(kind='full', board=board, format=FORMAT, version=version, directory=str(directory.resolve()),
                  compiled_source_commit=manifest['source_commit'], payload_mapping=SYSTEM_PAYLOADS, spi_bin=item(directory/BUNDLE_IMAGE),
                  archive=archive, files=files)
    write_json(output/'DELIVERY-FULL.json', result)
    return result


def read_ota_manifest(path):
    with Path(path).open('rb') as stream:
        header = stream.read(24)
        if len(header) != 24:
            raise ValueError('Truncated system OTA package')
        magic, length, signature, payload = struct.unpack('<8sIIQ', header)
        if magic not in (b'Y4PKG001', b'Y4PKG002', b'Y4PKG003', b'Y12PKG03') or not 0 < length <= 1048576 or signature != 64:
            raise ValueError('Not a recognized system OTA package')
        manifest = json.loads(stream.read(length))
        board = 'Y12' if magic == b'Y12PKG03' else 'Y4'
        if manifest.get('board', 'Y4') != board:
            raise ValueError('OTA header and manifest board differ')
        offset = (24+length+signature+4095)//4096*4096
        physical = manifest.get('compressed_bytes', payload) if magic == b'Y4PKG002' else payload
        if (manifest.get('type') != 'sysroot' or
                manifest.get('schema') != (2 if magic in (b'Y4PKG003', b'Y12PKG03') else 1) or
                Path(path).stat().st_size != offset+physical):
            raise ValueError('Invalid system OTA package length/type')
        version_name(manifest['version'])
        return manifest


def create_ota(package, output, jobs=8):
    package, output = Path(package), Path(output)
    manifest = read_ota_manifest(package)
    version = manifest['version']
    board = manifest.get('board', 'Y4')
    directory = output/'ota'
    directory.mkdir(parents=True, exist_ok=False)
    name = f'{board}-{version}.y4pkg'
    copy_file(package, directory/name, link=True)
    (directory/'README.txt').write_text(ota_readme(version, board), encoding='utf-8')
    write_checksums(directory)
    files = verify_directory(directory, 'ota', version, board)
    archive = archive_delivery(directory, output, 'ota', version, jobs, name, board)
    result = dict(kind='ota', board=board, version=version, directory=str(directory.resolve()), archive=archive,
                  system_package_sha256=files[name]['sha256'], files=files)
    write_json(output/'DELIVERY-OTA.json', result)
    return result


def verify_full_delivery(build_package, receipt):
    build_package = Path(build_package)
    directory = Path(receipt['directory'])
    manifest = json.loads((build_package/'MANIFEST.json').read_text())
    release = json.loads((build_package/'y4-release.json').read_text())
    if receipt['version'] != release['rootfs'] or receipt['compiled_source_commit'] != manifest['source_commit']:
        raise ValueError('Delivery/build identity mismatch')
    board = release.get('board', 'Y4')
    if board not in ('Y4', 'Y12') or receipt.get('board', 'Y4') != board or manifest.get('hardware') != board+'-6187':
        raise ValueError('Delivery board mismatch')
    files = verify_directory(directory, 'full', receipt['version'], board, bundle=True)
    if receipt['format'] != FORMAT or receipt.get('spi_bin') != files[BUNDLE_IMAGE]:
        raise ValueError('Delivery SPI BIN differs from its recorded input')
    for old, new in SYSTEM_PAYLOADS.items():
        if files[new] != manifest['files'][old]:
            raise ValueError('Delivery changed a compiled payload: '+old)
    archive = receipt['archive']
    if item(Path(archive['path'])) != {k: archive[k] for k in ('bytes', 'sha256')}:
        raise ValueError('Delivery ZIP checksum mismatch')
    verify_archive(archive['path'], files)
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=8)
    commands = parser.add_subparsers(dest='kind', required=True)
    full = commands.add_parser('full')
    full.add_argument('--build-package', type=Path, required=True)
    full.add_argument('--host-source', type=Path, default=Path(__file__).parent/'flash')
    full.add_argument('--output', type=Path, required=True)
    full.add_argument('--spi-bin', type=Path, required=True)
    ota = commands.add_parser('ota')
    ota.add_argument('--package', type=Path, required=True)
    ota.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.kind == 'full':
        result = create_full(args.build_package, args.host_source, args.output, args.jobs,
                             spi_bin=args.spi_bin)
        verify_full_delivery(args.build_package, result)
    else:
        result = create_ota(args.package, args.output, args.jobs)
        verify_archive(result['archive']['path'], result['files'])
    print(json.dumps(dict(version=result['version'], archive=result['archive'], files=len(result['files'])), indent=2))


if __name__ == '__main__':
    main()
