#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Exercise a built Y4 or Y12 daemon in an isolated ARM64 Debian fixture.

Run as root with --runtime pointing to a Debian ARM64 fixture containing
lib/, usr/lib/ and usr/bin/qemu-aarch64-static. The test copies those
runtime files into a temporary chroot and creates synthetic procfs data.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

RESOURCES = "/api/v1/system/resources"
EVENTS = "/api/v1/events"
VERSION = "/api/v1/version"
OTA = "/api/v1/update"
BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"
UPLOAD_LENGTH = 16  # shorter than even the OTA container header
CPU_INITIAL = "cpu  100 20 30 600 30 0 0 0 12 2\n"
# Delta 200 ticks, including 100 idle and 30 iowait. Guest columns overlap.
CPU_NEXT = "cpu  135 25 50 700 60 5 5 0 31 4\n"


def replace_file(path, data):
    replacement = path.with_suffix(".next")
    replacement.write_text(data, encoding="ascii")
    replacement.replace(path)


def set_memory(root, available):
    replace_file(root / "proc/meminfo", f"""MemTotal:        524288 kB
MemFree:          65536 kB
MemAvailable:    {available} kB
""")


def memory(available, percent):
    return {
        "total_bytes": 524288 * 1024,
        "available_bytes": available * 1024,
        "used_bytes": (524288 - available) * 1024,
        "usage_percent": percent,
    }


def get_request(path, body=b""):
    length = f"Content-Length: {len(body)}\r\n" if body else ""
    return f"GET {path} HTTP/1.1\r\nHost: localhost\r\n{length}\r\n".encode() + body


def connect(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(4)
    try:
        sock.connect(str(path))
    except BaseException:
        sock.close()
        raise
    return sock


def receive(sock):
    response = http.client.HTTPResponse(sock)
    response.begin()
    return response


def read_json(response):
    assert response.headers.get_content_type() == "application/json"
    return response.status, json.loads(response.read())


def get(path, endpoint=RESOURCES, body=b""):
    with connect(path) as sock:
        sock.sendall(get_request(endpoint, body))
        with receive(sock) as response:
            return read_json(response)


class Events:
    def __init__(self, response):
        self.response = response
        assert self.response.status == 200, self.response.status
        assert self.response.headers.get_content_type() == "text/event-stream"
        self.retry = None

    def next_event(self):
        fields = {}
        for _ in range(48):
            raw = self.response.readline(4096)
            assert raw, "event stream disconnected"
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line:
                if "data" in fields:
                    assert fields.get("event"), fields
                    return fields["event"], json.loads(fields["data"])
                fields.clear()
            elif not line.startswith(":"):
                name, sep, value = line.partition(":")
                if sep:
                    fields[name] = value.removeprefix(" ")
                    if name == "retry":
                        self.retry = int(fields[name])
        raise AssertionError("event stream did not provide a complete event")

    def matching(self, topic, predicate=lambda _: True, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            name, payload = self.next_event()
            if name == topic and predicate(payload):
                return payload
        raise AssertionError(f"no matching event for {topic!r}")

    def next_snapshot(self):
        return self.matching("system-resources")

    def expect_eof(self):
        for _ in range(24):
            if not self.response.readline(4096):
                return
        raise AssertionError("event stream stayed open after stopping the daemon")


@contextmanager
def event_stream(path):
    with connect(path) as sock:
        sock.sendall(get_request(EVENTS))
        with receive(sock) as response:
            yield Events(response)


def check_shape(sample, board):
    assert sample["schema"] == 1 and sample["device"] == board, sample
    assert type(sample["sampled_ms"]) is int and sample["sampled_ms"] >= 0, sample


def initial_events(stream, board):
    topics = {"system-resources", "fan-status", "temperature", "ota-status", "mcu-update"}
    if board == "Y12":
        topics.add("slot-power")
    values = {}
    for _ in range(18):
        topic, payload = stream.next_event()
        assert topic in topics, (board, topic)
        assert payload["schema"] == 1, payload
        values[topic] = payload
        if values.keys() == topics:
            return values
    raise AssertionError(f"{board}: did not receive initial topics {topics - values.keys()}")


def check_initial_topics(stream, board):
    values = initial_events(stream, board)
    check_shape(values["system-resources"], board)
    assert values["ota-status"]["busy"] is False
    assert values["mcu-update"]["running"] is False
    assert values["fan-status"]["device"] == board
    temps = values["temperature"]
    assert temps["device"] == board and "CPU" in temps["temperatures"]
    if board == "Y12":
        power = values["slot-power"]
        assert power["device"] == board
        assert [slot["slot"] for slot in power["slots"]] == list(range(1, 13))
    return values["system-resources"]


def fan_input(root, board):
    if board == "Y12":
        return root / "sys/bus/platform/devices/pwm-fan-out2/hwmon/hwmon0/fan1_input"
    return root / "sys/class/hwmon/hwmon0/fan1_input"


def fan_rpm(payload, board):
    if board == "Y12":
        first = payload["fans"][0]
        assert first["id"] == "FAN1"
        return first["rpm"]
    return payload["FAN1"]["rpm"] if payload["FAN1"]["valid"] else None


def check_fan_change(root, path, stream, board):
    replace_file(fan_input(root, board), "1234\n")
    event = stream.matching("fan-status", lambda value: fan_rpm(value, board) == 1234)
    status, payload = get(path, "/api/v1/fans")
    assert status == 200 and fan_rpm(payload, board) == fan_rpm(event, board)
    if board == "Y12":
        group = "pwm-fan-out2"

        def pwm(sample):
            return next(value["pwm_raw"] for value in sample["groups"] if value["id"] == group)

        body = json.dumps({"group": group, "percent": 33}).encode()
        request = f"POST /api/v1/fans HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(body)}\r\n\r\n"
        with connect(path) as sock:
            sock.sendall(request.encode() + body)
            status, payload = read_reply(sock)
        assert status == 200 and pwm(payload) == 84, (status, payload)
        event = stream.matching("fan-status", lambda value: pwm(value) == 84)
        assert pwm(get(path, "/api/v1/fans")[1]) == pwm(event)
        assert fan_input(root, board).with_name("pwm1").read_text().strip() == "84"


def check_idle_upgrade_events(stream):
    resources = 0
    for _ in range(18):
        topic, payload = stream.next_event()
        assert topic not in ("ota-status", "mcu-update"), (topic, payload)
        if topic == "system-resources":
            resources += 1
            if resources >= 3:
                return
    raise AssertionError("resource ticks stopped while checking idle update topics")


def all_request_slots(path, board):
    with ExitStack() as stack:
        requests = [stack.enter_context(connect(path)) for _ in range(8)]
        # Keep all eight inputs incomplete until both subscribers are active.
        for conn in requests:
            conn.sendall(get_request(VERSION)[:-2])
        for conn in requests:
            conn.sendall(b"\r\n")
        for conn in requests:
            with receive(conn) as response:
                status, payload = read_json(response)
                assert status == 200 and payload["device"] == board


def non_root_is_rejected(root, path):
    parent_mode, socket_mode = path.parent.stat().st_mode & 0o777, path.stat().st_mode & 0o777
    assert parent_mode == 0o700 and socket_mode == 0o600
    # In the fixture, remove only filesystem restrictions so this tests SO_PEERCRED.
    path.parent.chmod(0o711)
    path.chmod(0o666)
    try:
        pid = os.fork()
        if pid == 0:
            try:
                os.chroot(root)
                os.chdir("/")
                os.setgroups([])
                os.setgid(65534)
                os.setuid(65534)
                with connect("/run/y4-board/api.sock") as sock:
                    sock.sendall(get_request(EVENTS))
                    assert sock.recv(1) == b""
                os._exit(0)
            except (ConnectionResetError, BrokenPipeError):
                os._exit(0)
            except BaseException:
                os._exit(1)
        assert os.waitpid(pid, 0)[1] == 0, "non-root event connection was not rejected"
    finally:
        path.parent.chmod(parent_mode)
        path.chmod(socket_mode)


@contextmanager
def running_daemon(root, socket_path, log):
    with log.open("ab") as output:
        daemon = subprocess.Popen(
            ["/usr/bin/chroot", str(root), "/usr/bin/qemu-aarch64-static", "/y4-boardd"],
            stdout=output,
            stderr=output,
            start_new_session=True,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
        )
        try:
            for _ in range(80):
                assert daemon.poll() is None, daemon.returncode
                if socket_path.is_socket():
                    break
                time.sleep(0.05)
            assert socket_path.is_socket(), "daemon did not create the fixture socket"
            yield
        finally:
            if daemon.poll() is None:
                os.killpg(daemon.pid, signal.SIGTERM)
                try:
                    daemon.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    os.killpg(daemon.pid, signal.SIGKILL)
                    daemon.wait(timeout=4)
        assert not socket_path.exists(), "daemon left its listener behind"


def exercise(root, path, board):
    with event_stream(path) as left:
        initial = check_initial_topics(left, board)
        assert initial["cpu"] is None and initial["memory"] == memory(393216, 25), initial
        assert left.retry == 2000
        with event_stream(path) as right:
            check_initial_topics(right, board)
            status, error = get(path, EVENTS)
            assert status == 503 and error["error"] == "event_stream_limit", (status, error)
            all_request_slots(path, board)
            for endpoint in (EVENTS, RESOURCES):
                status, error = get(path, endpoint, b"X")
                assert status == 400 and error["error"] == "get_body_unavailable"
            non_root_is_rejected(root, path)
            replace_file(root / "proc/stat", CPU_NEXT)
            set_memory(root, 262144)
            updated = left.next_snapshot()
            for _ in range(3):
                if updated["cpu"] is not None:
                    break
                updated = left.next_snapshot()
            assert updated["cpu"] == {"usage_percent": 35}, updated
            assert updated["memory"] == memory(262144, 50), updated
            assert updated["sampled_ms"] > initial["sampled_ms"]
            mirrored = right.next_snapshot()
            while mirrored["sampled_ms"] < updated["sampled_ms"]:
                mirrored = right.next_snapshot()
            assert mirrored == updated, (updated, mirrored)
            assert get(path) == (200, updated), "GET and SSE disagree on the latest snapshot"
            next_sample = left.next_snapshot()
            assert 750 <= next_sample["sampled_ms"] - updated["sampled_ms"] <= 1600, next_sample
        # The second client closed. Read current data from a new connection.
        with event_stream(path) as reconnect:
            fresh = check_initial_topics(reconnect, board)
            assert fresh["sampled_ms"] >= next_sample["sampled_ms"]
            assert fresh["memory"] == memory(262144, 50)
        check_fan_change(root, path, left, board)
        check_idle_upgrade_events(left)
        if board == "Y4":
            assert get(path, "/api/v1/slot-power")[0] == 404
        print(f"{board}: initial topics, change and deduplication, metrics, cadence, peer credentials, connection limits passed")


def check_slow_reader(path, board):
    with event_stream(path) as healthy, connect(path) as stalled:
        stalled.sendall(get_request(EVENTS))
        # The response started. Leave the remainder in the socket buffer.
        assert stalled.recv(1) == b"H"
        before = healthy.next_snapshot()["sampled_ms"]
        latest = before
        deadline = time.monotonic() + 30
        reclaimed = False
        while time.monotonic() < deadline:
            sample = healthy.next_snapshot()
            check_shape(sample, board)
            assert sample["sampled_ms"] > latest
            latest = sample["sampled_ms"]
            with connect(path) as probe:
                probe.sendall(get_request(EVENTS))
                with receive(probe) as response:
                    if response.status == 200:
                        fresh = Events(response).next_snapshot()
                        assert fresh["sampled_ms"] >= latest
                        reclaimed = True
                    else:
                        assert read_json(response)[1]["error"] == "event_stream_limit"
            if reclaimed and latest - before >= 16_000:
                break
        assert reclaimed, "a non-reading subscriber kept its slot"
        assert latest - before >= 16_000, "the healthy stream did not outlive the request deadline"
        assert get(path, VERSION)[0] == 200
        print(f"{board}: slow subscriber released and healthy stream stayed open for {latest-before} ms")


def send_upload(sock, body):
    request = f"POST {OTA} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {UPLOAD_LENGTH}\r\n\r\n"
    sock.sendall(request.encode() + body)


def ota_state(root, path, busy):
    for _ in range(40):
        status, payload = get(path, OTA + "/status")
        assert status == 200, (status, payload)
        staged = list((root / "data/y4-board/ota").glob("*.y4pkg"))
        if payload["busy"] is busy and len(staged) == int(busy):
            return payload
        time.sleep(0.05)
    raise AssertionError(f"OTA busy={busy} and expected staging not reached: {payload}, {staged}")


def streamed_ota_state(stream, busy, timeout=8):
    return stream.matching("ota-status", lambda value: value["busy"] is busy, timeout)


def read_reply(sock):
    with receive(sock) as response:
        return read_json(response)


def check_upload_cleanup(root, path, board, idle_upload):
    with event_stream(path) as stream:
        check_initial_topics(stream, board)
        # Exercise excess data in the same socket read as the headers.
        with connect(path) as upload:
            send_upload(upload, b"x" * (UPLOAD_LENGTH + 1))
            with receive(upload) as response:
                status, payload = read_json(response)
                assert status == 400 and payload["error"] == "extra_body_bytes", (status, payload)
        ota_state(root, path, False)

        # Reach admission in a separate read, then overrun the remaining length.
        with connect(path) as upload:
            send_upload(upload, b"x")
            admitted = ota_state(root, path, True)
            assert streamed_ota_state(stream, True)["job"] == admitted["job"]
            upload.sendall(b"x" * UPLOAD_LENGTH)
            with receive(upload) as response:
                status, payload = read_json(response)
                assert status == 400 and payload["error"] == "extra_body_bytes", (status, payload)
            ota_state(root, path, False)
            streamed_ota_state(stream, False)

        # Confirm another upload can enter reception, then interrupt it mid-body.
        with connect(path) as upload:
            send_upload(upload, b"x")
            admitted = ota_state(root, path, True)
            assert streamed_ota_state(stream, True)["job"] == admitted["job"]
        ota_state(root, path, False)
        streamed_ota_state(stream, False)
        with connect(path) as upload:
            send_upload(upload, b"xx")
            admitted = ota_state(root, path, True)
            assert streamed_ota_state(stream, True)["job"] == admitted["job"]
            if idle_upload:
                upload.settimeout(36)
                with ThreadPoolExecutor(max_workers=1) as waiting:
                    reply = waiting.submit(read_reply, upload)
                    # Keep consuming the event stream throughout the upload wait.
                    streamed_ota_state(stream, False, timeout=36)
                    status, payload = reply.result()
                    assert status == 408 and payload["error"] == "upload_idle_timeout", (status, payload)
        ota_state(root, path, False)
        if idle_upload:
            with connect(path) as upload:
                send_upload(upload, b"x")
                ota_state(root, path, True)
                streamed_ota_state(stream, True)
            ota_state(root, path, False)
        streamed_ota_state(stream, False)
        assert get(path, VERSION)[0] == 200

    timeout = ", inactivity timeout" if idle_upload else ""
    print(f"{board}: OTA overrun (coalesced and fragmented), disconnect{timeout}, staging removal and readmission passed")


def configure_fixture(root, board):
    for directory in ("data", "proc/device-tree", "proc/sys/kernel/random", "run", "var/lib"):
        (root / directory).mkdir(parents=True)
    (root / "proc/device-tree/compatible").write_bytes(f"myt,{board.lower()}\0".encode())
    (root / "proc/sys/kernel/random/boot_id").write_text(BOOT_ID + "\n", encoding="ascii")
    (root / "proc/stat").write_text(CPU_INITIAL, encoding="ascii")
    set_memory(root, 393216)

    run = root / "run/y4-board"
    run.mkdir(mode=0o700)
    (run / "boot.lock").touch(mode=0o600)
    receipt = {"schema": 1, "attempt": 1, "max_attempts": 5, "status": 0,
               "completed": True, "network_safe": True, "phase": "complete", "boot_id": BOOT_ID}
    (run / "initialization.json").write_text(json.dumps(receipt), encoding="ascii")

    fake_fan = fan_input(root, board)
    fake_fan.parent.mkdir(parents=True)
    fake_fan.write_text("900\n", encoding="ascii")
    if board == "Y4":
        (fake_fan.parent / "name").write_text("pwmfan\n", encoding="ascii")
    else:
        (fake_fan.parent / "pwm1").write_text("51\n", encoding="ascii")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--board", choices=("Y4", "Y12"), required=True)
    parser.add_argument("--slow-reader", action="store_true", help="exercise backpressure for up to 30 seconds")
    parser.add_argument("--idle-upload", action="store_true", help="wait for the idle OTA upload deadline")
    parser.add_argument("daemon", type=Path)
    args = parser.parse_args()
    if os.geteuid():
        parser.error("root is required for the disposable ARM64 chroot")
    if not (args.runtime / "usr/bin/qemu-aarch64-static").is_file() or not args.daemon.is_file():
        parser.error("supply a Debian ARM64 fixture and a built daemon")
    with tempfile.TemporaryDirectory(prefix="y4-res-") as work:
        root = Path(work) / "root"
        shutil.copytree(args.runtime / "lib", root / "lib", symlinks=True)
        shutil.copytree(args.runtime / "usr/lib", root / "usr/lib", symlinks=True)
        (root / "usr/bin").mkdir()
        shutil.copy2(args.runtime / "usr/bin/qemu-aarch64-static", root / "usr/bin/qemu-aarch64-static")
        shutil.copy2(args.daemon, root / "y4-boardd")
        configure_fixture(root, args.board)
        path = root / "run/y4-board/api.sock"
        log = Path(work) / "daemon.log"
        try:
            with ExitStack() as subscribers:
                with running_daemon(root, path, log):
                    exercise(root, path, args.board)
                    check_upload_cleanup(root, path, args.board, args.idle_upload)
                    if args.slow_reader:
                        check_slow_reader(path, args.board)
                    stream = subscribers.enter_context(event_stream(path))
                    stream.next_snapshot()
                stream.expect_eof()
            with running_daemon(root, path, log):
                with event_stream(path) as stream:
                    renewed = check_initial_topics(stream, args.board)
                    assert renewed["memory"] == memory(262144, 50), renewed
            print(f"{args.board}: subscribers disconnected on daemon stop and resumed after restart")
        except BaseException:
            print(log.read_text(encoding="utf-8", errors="replace")[-4096:], file=sys.stderr)
            raise


if __name__ == "__main__":
    main()
