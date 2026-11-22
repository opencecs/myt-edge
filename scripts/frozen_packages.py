"""Verify frozen Debian inputs without installing packages or editing sources.

Only manifest entries with deb are frozen; transitive dependencies continue
to be resolved by the existing base/APT workflow. Call this before writing APT
configuration, then replace the matching name/version arguments with returned
absolute paths inside the staged root.
"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request

PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?$")
VERSION = re.compile(r"^[0-9][A-Za-z0-9.+:~_-]*$")
SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")


def _https_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("frozen package URL must be a single HTTPS URL")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment):
        raise ValueError("frozen package URL must use HTTPS without credentials or fragments")
    return value


def _specifications(manifest):
    if not isinstance(manifest, dict):
        raise ValueError("package manifest must be an object")
    packages = manifest.get("packages", [])
    if not isinstance(packages, list):
        raise ValueError("manifest packages must be a list")
    selected = []
    names = set()
    for entry in packages:
        if not isinstance(entry, dict):
            raise ValueError("package entry must be an object")
        name = entry.get("name")
        version = entry.get("version")
        if not isinstance(name, str) or not PACKAGE.fullmatch(name):
            raise ValueError("invalid package name")
        if name in names:
            raise ValueError("duplicate package declaration: " + name)
        names.add(name)
        if version is not None and (not isinstance(version, str) or not VERSION.fullmatch(version)):
            raise ValueError("invalid package version for " + name)
        if "deb" not in entry:
            continue
        if version is None or ":" in name:
            raise ValueError("frozen packages require a fixed version and unqualified package name")
        deb = entry["deb"]
        if not isinstance(deb, dict):
            raise ValueError("frozen deb declaration must be an object")
        digest = deb.get("sha256")
        size = deb.get("bytes")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError("frozen package requires a SHA256: " + name)
        if type(size) is not int or size <= 0:
            raise ValueError("frozen package requires a positive byte count: " + name)
        urls = [_https_url(deb.get("url"))]
        mirrors = deb.get("urls", [])
        if not isinstance(mirrors, list):
            raise ValueError("frozen package mirrors must be a list")
        for mirror in mirrors:
            mirror = _https_url(mirror)
            if mirror not in urls:
                urls.append(mirror)
        arch = entry.get("arch", entry.get("architecture"))
        if arch is not None and arch not in ("arm64", "all"):
            raise ValueError("frozen package target architecture must be arm64 or all")
        if ("arch" in entry and "architecture" in entry
                and entry["arch"] != entry["architecture"]):
            raise ValueError("conflicting package architecture declarations")
        selected.append(dict(name=name, version=version, arch=arch,
                             sha256=digest.lower(), bytes=size, urls=urls))
    return selected


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_bytes(path, spec):
    if path.is_symlink() or not path.is_file():
        raise ValueError("frozen package must be a regular file")
    if path.stat().st_size != spec["bytes"]:
        raise ValueError("frozen package size mismatch: " + spec["name"])
    if _hash(path) != spec["sha256"]:
        raise ValueError("frozen package SHA256 mismatch: " + spec["name"])


def _verify_metadata(path, spec):
    try:
        result = subprocess.run(
            ["dpkg-deb", "-f", str(path), "Package", "Version", "Architecture"],
            check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as error:
        raise ValueError("invalid Debian archive: " + spec["name"]) from error
    fields = {}
    for line in result.stdout.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            fields[key] = value
    if fields.get("Package") != spec["name"]:
        raise ValueError("Debian Package metadata mismatch: " + spec["name"])
    if fields.get("Version") != spec["version"]:
        raise ValueError("Debian Version metadata mismatch: " + spec["name"])
    arch = fields.get("Architecture")
    if arch not in ("arm64", "all") or (spec["arch"] and arch != spec["arch"]):
        raise ValueError("Debian Architecture metadata mismatch: " + spec["name"])


class _HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url, destination, expected_bytes):
    opener = urllib.request.build_opener(_HTTPSRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "Y4-frozen-packages/1"})
    with opener.open(request, timeout=60) as response:
        _https_url(response.geturl())
        total = 0
        with destination.open("wb") as output:
            while True:
                block = response.read(min(1024 * 1024, expected_bytes - total + 1))
                if not block:
                    break
                total += len(block)
                if total > expected_bytes:
                    raise ValueError("frozen package download exceeds declared byte count")
                output.write(block)
            output.flush()
            os.fsync(output.fileno())


def _cache_path(root):
    cache = root / "var/cache/apt/archives"
    if cache.is_symlink() or not cache.resolve().is_relative_to(root):
        raise ValueError("staged APT archive path escapes root or is symlinked")
    if cache.exists() and not cache.is_dir():
        raise ValueError("staged APT archive path is not a directory")
    return cache


def _publish(staged, cache, digest):
    """Create a new cache name exclusively, preserving every existing entry."""
    for suffix in range(10000):
        ending = "" if suffix == 0 else "-" + str(suffix)
        target = cache / ("y4-frozen-" + digest + ending + ".deb")
        try:
            os.link(staged, target)
            return target
        except FileExistsError:
            continue
    raise ValueError("unable to reserve a frozen package cache filename")


def prepare_frozen_packages(root, manifest, *, download=None):
    """Return {name: absolute_chroot_deb_path} after all inputs validate.

    download(url, destination_path, expected_bytes) may be injected by
    tests. Native dpkg-deb only reads control metadata; no package is installed
    or unpacked. Downloads are temporary until the entire group is verified.
    Existing cache entries are never overwritten, including invalid archives.
    """
    specs = _specifications(manifest)
    if not specs:
        return {}
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("staged package root must already exist")
    cache = _cache_path(root)
    cache.mkdir(parents=True, exist_ok=True)
    download = _download if download is None else download
    cached_files = sorted(p for p in cache.glob("*.deb") if p.is_file() and not p.is_symlink())
    ready = {}
    pending = []
    created = []
    with tempfile.TemporaryDirectory(prefix=".y4-frozen-", dir=cache) as temporary:
        for index, spec in enumerate(specs):
            for path in cached_files:
                if path.stat().st_size == spec["bytes"] and _hash(path) == spec["sha256"]:
                    _verify_metadata(path, spec)
                    ready[spec["name"]] = path
                    break
            else:
                staged = Path(temporary) / (str(index) + ".deb")
                errors = []
                for url in spec["urls"]:
                    try:
                        download(url, staged, spec["bytes"])
                        _verify_bytes(staged, spec)
                        _verify_metadata(staged, spec)
                        staged.chmod(0o644)
                        pending.append((spec, staged))
                        break
                    except (OSError, ValueError) as error:
                        staged.unlink(missing_ok=True)
                        errors.append(str(error))
                else:
                    raise ValueError("no verified frozen input for " + spec["name"]
                                     + ": " + "; ".join(errors))
        try:
            for spec, staged in pending:
                path = _publish(staged, cache, spec["sha256"])
                info = path.stat()
                created.append((path, info.st_dev, info.st_ino))
                ready[spec["name"]] = path
        except Exception:
            for path, device, inode in reversed(created):
                try:
                    info = path.lstat()
                    if (info.st_dev, info.st_ino) == (device, inode):
                        path.unlink()
                except FileNotFoundError:
                    pass
            raise
    return {name: "/" + str(path.relative_to(root)) for name, path in ready.items()}
