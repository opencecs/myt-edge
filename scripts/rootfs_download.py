"""Fetch and verify immutable package inputs in a shared content-addressed cache."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()

def good(path, entry):
    return path.is_file() and path.stat().st_size == entry['bytes'] and digest(path) == entry['sha256']

def fetch(entry, cache):
    target = cache / (entry['sha256'] + '.deb')
    if good(target, entry):
        return target
    url = entry['url']
    urls = [url]
    for mirror, origin in [('https://mirrors.ustc.edu.cn/debian/', 'https://deb.debian.org/debian/'),
                           ('https://mirrors.ustc.edu.cn/debian-security/', 'https://deb.debian.org/debian-security/'),
                           ('https://mirrors.ustc.edu.cn/docker-ce/', 'https://download.docker.com/')]:
        if url.startswith(mirror):
            urls.append(origin + url[len(mirror):])
    with tempfile.TemporaryDirectory(dir=cache) as tmp:
        candidate = Path(tmp) / 'download.deb'
        for source in urls:
            result = subprocess.run(['/usr/lib/apt/apt-helper', '-o','Acquire::Retries=2',
                                     'download-file',source,str(candidate),'SHA256:'+entry['sha256']],
                                     stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
            if result.returncode == 0 and good(candidate, entry):
                os.replace(candidate, target)
                return target
            candidate.unlink(missing_ok=True)
        raise RuntimeError(f"package download failed for {url}: {result.stderr[-700:]}")

def download_all(lock, cache):
    cache.mkdir(parents=True,exist_ok=True)
    entries = list(lock['common_packages'].values()) + [lock['vendor_files']['cix_desktop_members']]
    errors=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        jobs = {pool.submit(fetch,entry,cache):entry for entry in entries}
        for count, future in enumerate(concurrent.futures.as_completed(jobs),1):
            try:
                future.result()
            except Exception as exc:
                errors.append(str(exc))
                print(exc,flush=True)
            if count%100==0:
                print(f'{count}/{len(entries)} packages checked',flush=True)
    if errors:
        raise RuntimeError(f'{len(errors)} package inputs unavailable')

if __name__ == '__main__':
    download_all(json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2]))
