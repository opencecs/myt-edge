"""Compress the large sparse filesystem with pigz, then append standard ZIP entries."""
import subprocess
import zipfile
from pathlib import Path


def create(package, archive, jobs, log, large_entry='rootfs_sparse.ext4', level=1):
    package, archive = Path(package), Path(archive)
    if not 1 <= jobs <= 32:
        raise ValueError('ZIP compression threads must be 1..32')
    if not isinstance(level, int) or not 1 <= level <= 9:
        raise ValueError('ZIP compression level must be 1..9')
    entry = Path(large_entry)
    if entry.is_absolute() or '..' in entry.parts:
        raise ValueError('Invalid archive entry path')
    large = package / entry
    if entry.parent != Path('.'):
        # pigz strips directory components from its ZIP member name. Use the
        # standard ZIP writer for grouped payloads, including ZIP64 lengths.
        with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED,
                             compresslevel=level, allowZip64=True) as bundle:
            bundle.write(large, entry.as_posix())
            for source in sorted(package.rglob('*')):
                if source.is_file() and source != large:
                    bundle.write(source, source.relative_to(package).as_posix())
        Path(log).write_text('Grouped payload ZIP created with Python zlib/ZIP64.\n')
        return dict(format='zip-deflate', compressor='python-zlib', threads=1, level=level,
                    first_entry=entry.as_posix())
    with archive.open('xb') as output, Path(log).open('wb') as errors:
        subprocess.run(['pigz', '-K', '-'+str(level), '-p', str(jobs), '-b', '1024', '-c', '--', large.name],
                       cwd=package, stdout=output, stderr=errors, check=True)
    with zipfile.ZipFile(archive, 'a', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=level, allowZip64=True) as bundle:
        if bundle.namelist() != [large.name] or bundle.getinfo(large.name).file_size != large.stat().st_size:
            raise ValueError('Unexpected pigz ZIP entry name or ZIP64 length')
        for source in sorted(package.rglob('*')):
            if source.is_file() and source != large:
                bundle.write(source, str(source.relative_to(package)))
    return dict(format='zip-deflate', compressor='pigz', threads=jobs, level=level,
                block_kib=1024, parallel_entry=large.name)
