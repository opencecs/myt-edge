#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run MCU terminal-receipt regressions against an ARM64 Y4 daemon.

Use the same --runtime as test_resource_http.py. The Linux test host needs
root, ARM64 binfmt/QEMU, and a static ARM64 C compiler for the mock helper.
The disposable chroot contains synthetic files and no hardware devices.
"""

import argparse
import errno
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import tempfile
import time

import test_resource_http as wire

MCU = "/api/v1/mcu/firmware"
MCU_STATUS = "/api/v1/mcu/update"
CANCEL = "/api/v1/mcu/cancel"
MCU_BYTES = b"synthetic-mcu-v1" + bytes(2048)
UID = "0123456789abcdef01234567"
INSTALLED = {
    "schema": 1, "status": 0, "phase": "complete", "completed": True, "restored": True,
    "safe_for_network": True, "verified": True, "erase_attempted": True,
    "readback_verified": True, "uid": UID, "version": "9.1.2", "sequence": 2,
}


def fake_ota():
    manifest = b'{"board":"Y4","type":"sysroot"}'
    header = struct.pack("<8sIIQ", b"Y4PKG001", len(manifest), 64, 4096)
    return (header + manifest + bytes(64)).ljust(4096, b"\0") + bytes(4096)


def post(path, endpoint, body=b""):
    headers = f"POST {endpoint} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(body)}\r\n\r\n"
    with wire.connect(path) as sock:
        sock.sendall(headers.encode("ascii") + body)
        return wire.read_reply(sock)


def get(path, endpoint):
    status, payload = wire.get(path, endpoint)
    assert status == 200, (status, payload)
    return payload


def wait_for(read, description):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {description}")


def configure(root, runtime, daemon, helper):
    for directory in ("lib", "usr/lib"):
        shutil.copytree(runtime / directory, root / directory, symlinks=True)
    for directory in ("usr/bin", "usr/libexec/y4-board", "etc/y4-board", "usr/share/y4-board", "dev"):
        (root / directory).mkdir(parents=True)
    shutil.copy2(runtime / "usr/bin/qemu-aarch64-static", root / "usr/bin/qemu-aarch64-static")
    shutil.copy2(daemon, root / "y4-boardd")
    shutil.copy2(helper, root / "usr/libexec/y4-board/y4-mcu-maintain")
    wire.configure_fixture(root, "Y4")
    (root / "dev/null").touch()  # Plain file, used only as mock helper input.
    (root / "etc/y4-board/host-version").write_text("0.0.0-fixture\n", encoding="ascii")
    builtin = {"schema": 1, "sequence": 1, "sha256": "0" * 64}
    shared = root / "usr/share/y4-board"
    (shared / "mcu-builtin.json").write_text(json.dumps(builtin), encoding="ascii")
    (shared / "ota-public-key.bin").write_bytes(bytes.fromhex(
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    ))  # RFC 8032 test key. The fake OTA signature is deliberately invalid.
    (root / "data/y4-board/ota").mkdir(parents=True, mode=0o700)


def launch(root, path, action, receipt):
    marker = root / "fixture-mcu-pid"
    marker.unlink(missing_ok=True)
    (root / "fixture-mcu-action").write_text(action, encoding="ascii")
    target = root / "fixture-mcu-receipt.json"
    no_receipt = root / "fixture-mcu-no-receipt"
    target.unlink(missing_ok=True)
    no_receipt.unlink(missing_ok=True)
    if receipt is None:
        no_receipt.touch()
    else:
        target.write_text(json.dumps(receipt), encoding="ascii")
    status, payload = post(path, MCU, MCU_BYTES)
    assert status == 202 and payload["running"], (status, payload)
    pid = wait_for(lambda: marker.read_text() if marker.is_file() else "", "mock updater")
    return payload["job"], int(pid)


def interrupt(root, daemon, pid):
    assert daemon.pid != pid and os.getpgid(daemon.pid) == os.getpgid(pid)
    fd = os.pidfd_open(pid)
    try:
        assert (Path("/proc") / str(pid) / "root").resolve(strict=True) == root
        signal.pidfd_send_signal(fd, signal.SIGKILL)
    finally:
        os.close(fd)


def verify_blocked(root, path):
    ota_files = root / "data/y4-board/ota"
    mcu_files = root / "var/lib/y4-board"

    def files():
        return set(ota_files.glob("*.y4pkg")), set(mcu_files.glob("firmware-*.bin"))

    before = files()
    for endpoint, body in ((wire.OTA, fake_ota()), (MCU, MCU_BYTES)):
        status, payload = post(path, endpoint, body)
        assert status == 409, (endpoint, status, payload)
    assert files() == before, "blocked updates created staging files"
    assert get(path, wire.VERSION)["schema"] == 1


def terminal_event(events, job):
    return events.matching("mcu-update", lambda event: event["job"] == job and not event["running"])


def cases():
    cancelled = {
        "schema": 1, "status": -errno.ECANCELED, "phase": "cancelled", "completed": True,
        "restored": True, "safe_for_network": True, "verified": False, "erase_attempted": False,
    }
    yield "cancel before erase", "T", cancelled, "cancelled"
    yield "restore failed", "T", dict(cancelled, restored=False), "interrupted_unknown"
    yield "missing final receipt", "E", None, "interrupted_unknown"
    yield "SIGKILL while writing", "H", dict(INSTALLED, phase="writing", completed=False), "interrupted_unknown"
    yield "erase without readback", "S", dict(INSTALLED, readback_verified=False), "interrupted_unknown"
    yield "verified install", "S", INSTALLED, "complete"


def exercise(root, action, receipt, phase):
    path = root / "run/y4-board/api.sock"
    log = root.parent / "daemon.log"
    with wire.running_daemon(root, path, log) as daemon, wire.event_stream(path) as events:
        wire.initial_events(events, "Y4")
        job, pid = launch(root, path, action, receipt)
        if action == "T":
            code, reply = post(path, CANCEL)
            assert code == 202 and reply["cancel_requested"], (code, reply)
        elif action == "H":
            interrupt(root, daemon, pid)
        result = terminal_event(events, job)
        assert result["phase"] == phase, result
        assert get(path, MCU_STATUS)["phase"] == phase
        active = get(path, MCU)
        if phase == "interrupted_unknown":
            assert result["blocked"] and not result["verified"] and active["active"] is False, result
            verify_blocked(root, path)
        elif phase == "complete":
            assert not result["blocked"] and result["verified"], result
            assert active["job"] == job and active["uid"] == UID, active
        else:
            assert not result["blocked"] and result["status"] == -errno.ECANCELED, result
            code, reply = post(path, wire.OTA, fake_ota())
            assert code == 202, (code, reply)
            rejected = events.matching("ota-status", lambda event: event["job"] == reply["job"] and not event["busy"])
            assert rejected["phase"] == "failed" and rejected["status"] == -errno.EKEYREJECTED, rejected
            retried, _ = launch(root, path, "S", INSTALLED)
            result = terminal_event(events, retried)
            assert retried != job and result["phase"] == "complete" and result["verified"], result

    if phase == "interrupted_unknown":
        with wire.running_daemon(root, path, log):
            resumed = get(path, MCU_STATUS)
            assert resumed["job"] == job and resumed["phase"] == phase and resumed["blocked"], resumed
            verify_blocked(root, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--cc", default="aarch64-linux-gnu-gcc")
    parser.add_argument("daemon", type=Path)
    args = parser.parse_args()
    if os.geteuid():
        parser.error("root is required for the disposable ARM64 chroot")
    with tempfile.TemporaryDirectory(prefix="y4-mcu-") as scratch:
        scratch = Path(scratch)
        helper = scratch / "mock-mcu"
        source = Path(__file__).with_name("test_mcu_update_helper.c")
        subprocess.run([args.cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-O2",
                        "-static", "-o", str(helper), str(source)], check=True)
        for name, action, receipt, phase in cases():
            with tempfile.TemporaryDirectory(dir=scratch) as run:
                root = Path(run) / "root"
                configure(root, args.runtime, args.daemon, helper)
                try:
                    exercise(root, action, receipt, phase)
                except BaseException:
                    log = Path(run) / "daemon.log"
                    if log.is_file():
                        print(log.read_text(errors="replace")[-4096:])
                    raise
                print(f"Y4: {name} → {phase}", flush=True)


if __name__ == "__main__":
    main()
