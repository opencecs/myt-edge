#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Install native network assets into an explicit disposable Debian image tree.
No service/tool is executed. Never use this as a live-host installer.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

HERE = Path(__file__).resolve().parent
KNOWN_PHYSICAL = {"eth0", "eth1", "eth2", "y4lan", "y4sfp1", "y4sfp2", "y12lan", "y12sfp1", "y12sfp2"}
POLICY_UNIT = "y4-network-policy.service"
READY_UNIT = "y4-network-ready.service"

def _sha(data):
    return hashlib.sha256(data).hexdigest()

def _target(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("relative image path required")
    target = root / rel
    # All installed destinations use /etc, /usr and /var, avoiding the SDK's
    # /lib -> /usr symlink. Reject an escaping parent rather than follow it.
    parent = target.parent.resolve()
    if parent != root and root not in parent.parents:
        raise ValueError("image destination escapes staging root: " + relative)
    if target.is_symlink():
        raise ValueError("refuse to replace symlink: " + relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target

def _write(root, relative, data, mode=0o644):
    target = _target(root, relative)
    fd, temporary = tempfile.mkstemp(prefix=".y4-network-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), 0, 0)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"path": relative, "sha256": _sha(data), "bytes": len(data), "mode": oct(mode)}

def _ifupdown_without_physical(text, ensure_loopback=False):
    """Remove only the explicitly owned names from vendor ifupdown syntax."""
    out = []
    dropping = False
    for line in text.splitlines(keepends=True):
        command = re.match(r"^\s*(iface|mapping|auto|allow-[A-Za-z0-9_-]+|source|source-directory)\s+(.*)", line)
        if command:
            key, tail = command.groups()
            content, marker, comment = tail.partition("#")
            words = content.split()
            dropping = key in ("iface", "mapping") and bool(words) and words[0] in KNOWN_PHYSICAL
            if dropping:
                continue
            if key == "auto" or key.startswith("allow-"):
                keep = [name for name in words if name not in KNOWN_PHYSICAL]
                if keep:
                    indent = line[:len(line) - len(line.lstrip())]
                    ending = "\n" if line.endswith("\n") else ""
                    out.append(indent + key + " " + " ".join(keep) + (" #" + comment.rstrip("\n") if marker else "") + ending)
                elif marker:
                    out.append("#" + comment.rstrip("\n") + "\n")
                continue
        if not dropping:
            out.append(line)
    result = "".join(out)
    # The frozen SDK's top-level interfaces file declares auto lo but omits a
    # loopback method. Install the normal Debian method during that migration.
    if ensure_loopback and not re.search(r"^\s*iface\s+lo\s+inet\s+loopback\b", result, re.M):
        result += "\niface lo inet loopback\n"
    return result

def network_names(data, board):
    if board == "Y4":
        return data
    if board != "Y12":
        raise ValueError("Unsupported network profile board")
    return re.sub(rb"\by4(lan|sfp[12])\b", rb"y12\1", data)


def network_profile(data, board):
    data = network_names(data, board)
    return data.replace(b"id=Y4 ", b"id=Y12 ") if board == "Y12" else data


def stage_network(root, *, source=HERE, enable_policy=True, retire_vendor=True, install_assets=True, board="Y4"):
    """Pure file staging API used by board/install.py; returns a manifest."""
    root = Path(root).resolve()
    source = Path(source).resolve()
    if not root.is_absolute() or root == Path("/") or root == source or root in source.parents or source in root.parents:
        raise ValueError("an independent image staging directory is required")
    release = root / "etc/os-release"
    if not release.is_file() or not re.search(r'^ID=(?:"debian"|debian)$', release.read_text(), re.M):
        raise ValueError("Debian image root required")
    if not re.search(r'^VERSION_ID="?12"?$', release.read_text(), re.M):
        raise ValueError("Debian12 image root required")
    result = {"schema": 1, "network_manager": "NetworkManager", "installed": [],
              "retired_vendor_files": [], "unchanged_custom_files": [],
              "resolver_file_touched": False, "services_executed": False, "payload_assets_staged": bool(install_assets)}
    runtime = _target(root, "var/lib/y4-board/.staging-permission-check").parent
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime.chmod(0o700)
    pins = json.loads((source / "base-network-inputs.json").read_text())["files"]
    loopback_files = [root / "etc/network/interfaces", *sorted((root / "etc/network/interfaces.d").glob("*"))]
    loopback_present = any(p.is_file() and not p.is_symlink() and re.search(
        r"^\s*iface\s+lo\s+inet\s+loopback\b", p.read_text(errors="replace"), re.M) for p in loopback_files)
    if retire_vendor:
        for relative, row in pins.items():
            old = root / relative
            if not old.is_file() or old.is_symlink():
                continue
            raw = old.read_bytes()
            if _sha(raw) != row["sha256"]:
                result["unchanged_custom_files"].append(relative)
                continue
            archive = "var/lib/y4-board/migration/network/" + relative
            backup = root / archive
            if not backup.exists():
                _write(root, archive, raw, 0o600)
            elif backup.is_symlink() or not backup.is_file() or _sha(backup.read_bytes()) != row["sha256"]:
                raise ValueError("original vendor backup has unexpected contents: " + archive)
            if row["kind"] == "ifupdown":
                updated = _ifupdown_without_physical(raw.decode("utf8"),
                    ensure_loopback=relative == "etc/network/interfaces" and not loopback_present).encode("utf8")
                _write(root, relative, updated, old.stat().st_mode & 0o777)
            else:
                # Only a byte-for-byte pinned vendor profile can be retired.
                # Unrelated/current administrator profiles stay untouched.
                old.unlink()
            result["retired_vendor_files"].append({"path": relative, "before_sha256": row["sha256"], "backup": archive})
    dhcpcd_conf = root / "etc/dhcpcd.conf"
    if dhcpcd_conf.is_file() and not dhcpcd_conf.is_symlink():
        # The base image runs dhcpcd, a second DHCP manager. Exclude the Y4
        # interfaces under BOTH names: the transitional kernel names (before
        # the .link rename, and after a factory driver rebind recreates the
        # netdev) and the final .link names.
        deny = network_names(b"denyinterfaces y4lan y4sfp1 y4sfp2 bond10g eth0 eth1 eth2", board).decode("ascii")
        existing = dhcpcd_conf.read_text(errors="replace")
        if deny not in existing:
            updated = existing.rstrip("\n") + (
                "\n\n# Y4 interfaces are owned by NetworkManager only; the transitional\n"
                "# kernel names are excluded so the udev rename never races a lease.\n"
                + deny + "\n")
            _write(root, "etc/dhcpcd.conf", updated.encode(),
                   dhcpcd_conf.stat().st_mode & 0o777)
            result["dhcpcd_denyinterfaces"] = deny
    dispatcher = root / "etc/NetworkManager/dispatcher.d/01-ifupdown"
    if dispatcher.is_file() and not dispatcher.is_symlink():
        # NetworkManager is the single connection manager and ifupdown owns no
        # interface on this image. The legacy dispatcher would run run-parts on
        # every NM event and answers the modern reapply event with "unknown
        # action" status 1, printing two warnings per event (~1700 per boot
        # while the policy daemon converges). Archive it with the other
        # retired vendor files.
        archive = "var/lib/y4-board/migration/network/etc/NetworkManager/dispatcher.d/01-ifupdown"
        backup = root / archive
        if not backup.exists():
            _write(root, archive, dispatcher.read_bytes(), 0o600)
        elif backup.is_symlink() or not backup.is_file():
            raise ValueError("unexpected vendor dispatcher backup: " + archive)
        dispatcher.unlink()
        result["retired_vendor_files"].append({"path": "etc/NetworkManager/dispatcher.d/01-ifupdown", "backup": archive})
    if install_assets:
        groups = [
            ("profiles/*.nmconnection", "etc/NetworkManager/system-connections", 0o600),
            ("links/*.link", "usr/lib/systemd/network", 0o644),
        ]
        for pattern, directory, mode in groups:
            for p in sorted(source.glob(pattern)):
                data = p.read_bytes()
                if p.suffix == ".nmconnection":
                    data = network_profile(data, board)
                else:
                    data = network_names(data, board)
                result["installed"].append(_write(root, directory + "/" + p.name, data, mode))
        for p in sorted((source / "conf").glob("*.conf")):
            directory = "etc/sysctl.d" if p.name == "70-y4-multihome.conf" else "etc/NetworkManager/conf.d"
            result["installed"].append(_write(root, directory + "/" + p.name, network_names(p.read_bytes(), board)))
        for p in sorted((source / "systemd").rglob("*.conf")):
            relative = str(p.relative_to(source / "systemd"))
            result["installed"].append(_write(root, "etc/systemd/system/" + relative, p.read_bytes()))
        result["installed"].append(_write(root, "usr/lib/systemd/system/" + READY_UNIT,
                                          (source / "systemd" / READY_UNIT).read_bytes()))
        result["installed"].append(_write(root, "usr/lib/systemd/system/" + POLICY_UNIT,
                                          (source / "systemd" / POLICY_UNIT).read_bytes()))
    if enable_policy:
        wants = _target(root, "etc/systemd/system/multi-user.target.wants/.staging-enable-check").parent
        wants.mkdir(parents=True, exist_ok=True)
        link = wants / POLICY_UNIT
        expected = "/usr/lib/systemd/system/" + POLICY_UNIT
        if link.is_symlink():
            if str(link.readlink()) != expected:
                raise ValueError("unexpected policy enablement link")
        elif link.exists():
            raise ValueError("unexpected regular policy enablement file")
        else:
            link.symlink_to(expected)
    _write(root, "var/lib/y4-board/migration/network-staging.json",
           (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode(), 0o600)
    return result

def precheck(root, legacy_units=()):
    """Read-only conflict checks that must pass before dpkg unpacks the
    package, so an installation never aborts after side effects happened.
    Mirrors the enablement and legacy-mask decisions made during staging."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("image root required")
    expected = "/usr/lib/systemd/system/" + POLICY_UNIT
    link = root / "etc/systemd/system/multi-user.target.wants" / POLICY_UNIT
    if link.is_symlink():
        if str(link.readlink()) != expected:
            raise ValueError("unexpected policy enablement link")
    elif link.exists():
        raise ValueError("unexpected regular policy enablement file")
    for name in legacy_units:
        path = root / "etc/systemd/system" / name
        if path.is_symlink():
            continue
        if path.exists():
            raise ValueError("legacy unit mask would overwrite a regular administrator unit: " + str(path))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--image-staging", action="store_true", required=True)
    parser.add_argument("--board", choices=("Y4", "Y12"), default="Y4")
    args = parser.parse_args()
    print(json.dumps(stage_network(args.root, board=args.board), indent=2))

if __name__ == "__main__":
    main()
