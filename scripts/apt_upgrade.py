#!/usr/bin/env python3
"""Refresh packages inside a staged Debian root filesystem.

The manifest owns the declared Debian suites and named extra repositories.
Other repositories are preserved. Validate all declarations and pinned extra
keyrings before changing source files; package arguments never enter a shell.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from frozen_packages import prepare_frozen_packages

QEMU = Path("/usr/bin/qemu-aarch64-static")
BINDS = ("proc", "sys", "dev")
DEFAULT_PACKAGES = ["btop", "htop", "ffmpeg"]
TOKEN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.+_-]*$")
PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?$")
VERSION = re.compile(r"^[0-9][A-Za-z0-9.+:~_-]*$")
SOURCE_LINE = re.compile(r"^\s*(deb|deb-src)\s+(?:\[[^\]]*\]\s+)?(\S+)\s+(\S+)(?:\s|$)")
DEBIAN_MIRRORS = {
    "deb.debian.org", "security.debian.org", "ftp.debian.org",
    "mirrors.ustc.edu.cn",
}


def run(argv, **kw):
    print("+", " ".join(str(a) for a in argv), flush=True)
    return subprocess.run([str(a) for a in argv], check=True, **kw)


def _url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("repository URL must be a single HTTP(S) URL")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("unsupported repository URL: " + value)
    return value.rstrip("/")


def _token(value, label):
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise ValueError("invalid " + label)
    return value


def _components(values):
    if not isinstance(values, list) or not values:
        raise ValueError("repository components must be a non-empty list")
    return [_token(value, "repository component") for value in values]


def _debian_url(value):
    parsed = urllib.parse.urlsplit(value)
    return (parsed.hostname in DEBIAN_MIRRORS
            and parsed.path.rstrip("/") in ("/debian", "/debian-security"))


def _target(root, relative):
    target = root / relative
    # A staged absolute symlink must never make a host-side write escape root.
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("unsafe staged APT path: " + str(target))
    return target


def _remove_list_entries(text, replaced):
    result = []
    for line in text.splitlines(keepends=True):
        match = SOURCE_LINE.match(line)
        if match and match[1] == "deb" and replaced(match[2], match[3]):
            continue
        result.append(line)
    return "".join(result)


def _remove_deb822_entries(text, replaced):
    """Retain unrelated stanzas, including source-only and disabled entries."""
    result = []
    changed = False
    for block in re.split(r"\n[ \t]*\n", text):
        fields = {}
        last = None
        for line in block.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line[0].isspace() and last:
                fields[last] += " " + line.strip()
            elif ":" in line:
                key, value = line.split(":", 1)
                last = key.lower()
                fields[last] = value.strip()
        if fields.get("enabled", "yes").lower() == "no" or "deb" not in fields.get("types", "").split():
            result.append(block)
            continue
        uris = fields.get("uris", "").split()
        suites = fields.get("suites", "").split()
        removed = [suite for suite in suites if any(replaced(uri, suite) for uri in uris)]
        if not removed:
            result.append(block)
            continue
        # Splitting a mixed stanza can subtly change options or source access.
        # Reject it before any write rather than losing unknown repositories.
        if (fields.get("types", "").split() != ["deb"]
                or any(not replaced(uri, suite) for suite in removed for uri in uris)):
            raise ValueError("mixed Deb822 stanza overlaps managed sources; split it before building")
        changed = True
        kept = [suite for suite in suites if suite not in removed]
        if kept:
            block = re.sub(r"(?mi)^Suites:[^\n]*(?:\n[ \t]+[^\n]*)*",
                           "Suites: " + " ".join(kept), block)
            result.append(block)
    if not changed:
        return text
    return "\n\n".join(block for block in result if block.strip()) + ("\n" if any(b.strip() for b in result) else "")


def _write_files(writes):
    """Atomic per-file replace; roll back earlier files if a later write fails."""
    previous = {}
    completed = []
    temporary = []
    try:
        for target, data in writes.items():
            previous[target] = (target.read_bytes(), target.stat().st_mode & 0o777) if target.exists() else None
            if previous[target] == (data, 0o644):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".y4-apt-", dir=target.parent)
            temporary.append(Path(name))
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, 0o644)
            os.replace(name, target)
            completed.append(target)
    except Exception:
        for target in reversed(completed):
            saved = previous[target]
            if saved is None:
                target.unlink()
            else:
                target.write_bytes(saved[0])
                target.chmod(saved[1])
        raise
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)


def install_manifest_packages(root, manifest):
    """Configure declared sources and return separate apt package arguments."""
    root = Path(root).resolve()
    packages = []
    for package in manifest.get("packages", []):
        name = package["name"]
        version = package.get("version")
        if not isinstance(name, str) or not PACKAGE.fullmatch(name):
            raise ValueError("invalid package name")
        if version is not None and (not isinstance(version, str) or not VERSION.fullmatch(version)):
            raise ValueError("invalid package version for " + name)
        packages.append(name + ("=" + version if version else ""))

    primary = []
    for source in manifest.get("sources", []):
        if not isinstance(source, str):
            raise ValueError("sources entries must be URL suite components strings")
        parts = source.split()
        if len(parts) < 3:
            raise ValueError("source requires URL, suite and components")
        uri = _url(parts[0])
        if not _debian_url(uri):
            raise ValueError("primary sources must use a supported Debian archive; use sources_extra for other repositories")
        suite = _token(parts[1], "repository suite")
        components = _components(parts[2:])
        primary.append((uri, suite, components))
    if len({(uri, suite) for uri, suite, _ in primary}) != len(primary):
        raise ValueError("duplicate primary repository")
    if primary and not _target(root, "usr/share/keyrings/debian-archive-keyring.gpg").is_file():
        raise ValueError("Debian archive keyring is missing from staged rootfs")

    extras = []
    names = set()
    for source in manifest.get("sources_extra", []):
        name = source["name"]
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name)
                or name == "y4-debian" or name in names):
            raise ValueError("invalid or duplicate extra repository name")
        names.add(name)
        uri = _url(source["url"])
        suite = _token(source["suite"], "repository suite")
        components = _components(source.get("components", ["stable"]))
        digest = source.get("keyring_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError("extra repository requires a pinned keyring_sha256: " + name)
        keyurl = _url(source.get("keyring_url", uri + "/gpg"))
        if urllib.parse.urlsplit(keyurl).scheme != "https":
            raise ValueError("extra keyring URL must use HTTPS")
        extras.append((name, uri, suite, components, keyurl, digest.lower()))

    if len({(uri, suite) for _, uri, suite, _, _, _ in extras}) != len(extras):
        raise ValueError("duplicate extra repository")
    primary_suites = {suite for _, suite, _ in primary}
    extra_repositories = {(uri, suite) for _, uri, suite, _, _, _ in extras}

    def replaced(uri, suite):
        return ((_debian_url(uri) and suite in primary_suites)
                or (uri.rstrip("/"), suite) in extra_repositories)

    source_paths = [root / "etc/apt/sources.list"]
    sources_d = root / "etc/apt/sources.list.d"
    source_paths += sorted(sources_d.glob("*.list")) + sorted(sources_d.glob("*.sources"))
    texts = {}
    for path in source_paths:
        path = _target(root, path.relative_to(root))
        if not path.exists():
            continue
        old = path.read_text()
        new = (_remove_deb822_entries(old, replaced) if path.suffix == ".sources"
               else _remove_list_entries(old, replaced))
        if new != old:
            texts[path] = new

    def add_lines(relative, lines):
        path = _target(root, relative)
        old = texts.get(path, path.read_text() if path.exists() else "")
        texts[path] = (old.rstrip() + "\n" if old.strip() else "") + "\n".join(lines) + "\n"

    if primary:
        add_lines("etc/apt/sources.list.d/y4-debian.list", [
            "deb [arch=arm64 signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] "
            + uri + " " + suite + " " + " ".join(components)
            for uri, suite, components in primary
        ])
    keys = {}
    for name, uri, suite, components, keyurl, digest in extras:
        target = _target(root, "etc/apt/keyrings/" + name + ".asc")
        # Existing frozen keys can be used without network access.
        data = target.read_bytes() if target.is_file() else b""
        if hashlib.sha256(data).hexdigest() != digest:
            with urllib.request.urlopen(keyurl, timeout=60) as response:
                if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                    raise ValueError("extra keyring redirected away from HTTPS")
                data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024 or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("keyring digest mismatch: " + keyurl)
        keys[target] = data
        add_lines("etc/apt/sources.list.d/" + name + ".list", [
            "deb [arch=arm64 signed-by=/etc/apt/keyrings/" + name + ".asc] "
            + uri + " " + suite + " " + " ".join(components)
        ])
    writes = {path: text.encode() for path, text in texts.items()}
    writes.update(keys)
    _write_files(writes)
    return packages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--fetch", type=Path,
                    help="directory holding the rootfs download manifest (inputs-apt.json)")
    args = ap.parse_args()
    root = args.root.resolve()
    if root == Path("/") or not root.is_dir():
        raise ValueError("--root must be an existing staged rootfs, not the host root")

    packages = DEFAULT_PACKAGES
    frozen = {}
    if args.fetch and (args.fetch / "inputs-apt.json").is_file():
        manifest = json.loads((args.fetch / "inputs-apt.json").read_text())
        frozen = prepare_frozen_packages(root, manifest)
        packages = install_manifest_packages(root, manifest) or packages
        packages = [arg for arg in packages if arg.split("=", 1)[0] not in frozen]

    shutil.copy2(QEMU, root / "usr/bin/qemu-aarch64-static")
    shutil.copy2("/etc/resolv.conf", root / "etc/resolv.conf")
    mounts = []
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    prefix = ["chroot", str(root), "/usr/bin/qemu-aarch64-static"]
    try:
        for name in BINDS:
            target = root / name
            run(["mount", "--bind", f"/{name}", str(target)])
            mounts.append(target)
        run(prefix + ["/usr/bin/apt-get", "update"], env=env)
        run(prefix + ["/usr/bin/apt-get", "-y",
                      "-o", "Dpkg::Options::=--force-confdef",
                      "-o", "Dpkg::Options::=--force-confold", "upgrade"], env=env)
        if packages:
            run(prefix + ["/usr/bin/apt-get", "-y", "--allow-downgrades", "install", *packages], env=env)
        if frozen:
            # Reinstall even if upgrade selected the same version: consume the
            # exact validated DEB bytes, not another repository's equal version.
            run(prefix + ["/usr/bin/apt-get", "-y", "--allow-downgrades",
                          "--reinstall", "install", *frozen.values()], env=env)
        audit = run(prefix + ["/usr/bin/dpkg", "--audit"], capture_output=True, text=True)
        if audit.stdout.strip():
            raise ValueError("staged dpkg audit failed: " + audit.stdout.strip())
    finally:
        for target in reversed(mounts):
            subprocess.run(["umount", str(target)], check=False)
        (root / "usr/bin/qemu-aarch64-static").unlink(missing_ok=True)
    print("apt upgrade stage complete", flush=True)


if __name__ == "__main__":
    sys.exit(main())
