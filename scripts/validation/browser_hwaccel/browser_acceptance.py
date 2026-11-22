#!/usr/bin/env python3
"""Visible Chromium video checks using Python standard library only.

Runs only when explicitly invoked on the test device. Creates a fresh output
directory and owns one new browser session/process group. Never attaches to or
terminates an existing browser. No system configuration is changed.
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request

MAX_MESSAGE = 32 * 1024 * 1024
CASES = {"h264":"h264.mp4", "vp9":"vp9.webm", "vp8":"vp8.webm", "h264-mse":"h264-mse.mp4"}


def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def dump(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    os.replace(temporary, path)


class WebSocket:
    """RFC 6455 client supporting partial reads, fragmentation and ping/pong."""
    def __init__(self, url, timeout=10):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "ws" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("CDP must use a loopback WebSocket")
        self.sock = socket.create_connection((parsed.hostname, parsed.port or 80), timeout)
        self.buffer = bytearray()
        self.fragments = bytearray()
        self.fragment_opcode = None
        key = base64.b64encode(os.urandom(16)).decode()
        route = parsed.path or "/"
        if parsed.query:
            route += "?" + parsed.query
        request = ("GET " + route + " HTTP/1.1\r\nHost: " + parsed.netloc
                   + "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: "
                   + key + "\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(request.encode())
        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in self.buffer:
            self._fill(deadline)
            if len(self.buffer) > 65536:
                raise ValueError("oversized WebSocket handshake")
        header, remainder = bytes(self.buffer).split(b"\r\n\r\n", 1)
        self.buffer = bytearray(remainder)
        lines = header.decode("latin1").split("\r\n")
        if lines[0].split()[1] != "101":
            raise ValueError("WebSocket upgrade refused: " + lines[0])
        headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
        headers = {k.lower(): v.strip() for k, v in headers.items()}
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise ValueError("WebSocket accept mismatch")

    def _fill(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("WebSocket receive timeout")
        self.sock.settimeout(remaining)
        try:
            data = self.sock.recv(65536)
        except socket.timeout as error:
            raise TimeoutError("WebSocket receive timeout") from error
        if not data:
            raise EOFError("WebSocket closed")
        self.buffer.extend(data)

    def send_frame(self, opcode, payload):
        mask = os.urandom(4)
        size = len(payload)
        if size < 126:
            header = bytes([0x80 | opcode, 0x80 | size])
        elif size < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", size)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", size)
        encoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.sock.sendall(header + mask + encoded)

    def send(self, message):
        self.send_frame(1, json.dumps(message, separators=(",", ":")).encode())

    def _frame(self):
        b = self.buffer
        if len(b) < 2:
            return None
        fin, opcode, masked, size = bool(b[0] & 0x80), b[0] & 15, bool(b[1] & 0x80), b[1] & 127
        if b[0] & 0x70:
            raise ValueError("unsupported WebSocket reserved bits")
        offset = 2
        if size == 126:
            if len(b) < 4:
                return None
            size, offset = struct.unpack("!H", b[2:4])[0], 4
        elif size == 127:
            if len(b) < 10:
                return None
            size, offset = struct.unpack("!Q", b[2:10])[0], 10
        if size > MAX_MESSAGE:
            raise ValueError("oversized CDP message")
        if opcode >= 8 and (not fin or size > 125):
            raise ValueError("invalid WebSocket control frame")
        if masked:
            if len(b) < offset + 4:
                return None
            mask = b[offset:offset+4]
            offset += 4
        if len(b) < offset + size:
            return None
        payload = bytes(b[offset:offset+size])
        del b[:offset+size]
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return fin, opcode, payload

    def receive(self, timeout=1):
        deadline = time.monotonic() + timeout
        while True:
            frame = self._frame()
            if frame is None:
                self._fill(deadline)
                continue
            fin, opcode, payload = frame
            if opcode == 8:
                raise EOFError("WebSocket peer closed")
            if opcode == 9:
                self.send_frame(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode in (1, 2):
                if self.fragment_opcode is not None:
                    raise ValueError("interleaved fragmented WebSocket messages")
                self.fragment_opcode = opcode
                self.fragments = bytearray(payload)
            elif opcode == 0 and self.fragment_opcode is not None:
                self.fragments.extend(payload)
            else:
                raise ValueError("unexpected WebSocket opcode")
            if len(self.fragments) > MAX_MESSAGE:
                raise ValueError("oversized fragmented CDP message")
            if fin:
                opcode = self.fragment_opcode
                message = bytes(self.fragments)
                self.fragment_opcode = None
                self.fragments.clear()
                if opcode != 1:
                    raise ValueError("CDP returned a binary message")
                return json.loads(message.decode())

    def close(self):
        self.sock.close()


class CDP:
    def __init__(self, url, events_path, expected_boot):
        self.ws = WebSocket(url)
        self.sequence = 0
        self.responses = {}
        self.events = []
        self.log = events_path.open("w")
        self.commands = events_path.with_name("cdp-commands.jsonl").open("w")
        self.expected_boot = expected_boot

    def check_boot(self):
        if boot_id() != self.expected_boot:
            raise RuntimeError("boot ID changed; aborting this test epoch")

    def read(self, timeout):
        self.check_boot()
        message = self.ws.receive(timeout)
        if "id" in message:
            self.responses[message["id"]] = message
        else:
            event = {"utc": utc(), "monotonic": time.monotonic(), **message}
            self.events.append(event)
            self.log.write(json.dumps(event, separators=(",", ":")) + "\n")
            self.log.flush()
        return message

    def call(self, method, params=None, session=None, timeout=15):
        self.check_boot()
        self.sequence += 1
        identity = self.sequence
        request = {"id": identity, "method": method, "params": params or {}}
        if session:
            request["sessionId"] = session
        started = time.monotonic()
        self.commands.write(json.dumps({"utc":utc(),"phase":"send",**request}) + "\n")
        self.commands.flush()
        self.ws.send(request)
        deadline = time.monotonic() + timeout
        while identity not in self.responses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("CDP method timed out: " + method)
            try:
                self.read(remaining)
            except TimeoutError as error:
                self.commands.write(json.dumps({"utc":utc(),"phase":"timeout","id":identity,"method":method,"seconds":time.monotonic()-started}) + "\n")
                self.commands.flush()
                raise TimeoutError("CDP method timed out: " + method) from error
        response = self.responses.pop(identity)
        self.commands.write(json.dumps({"utc":utc(),"phase":"return","id":identity,"method":method,
                                        "seconds":time.monotonic()-started,"error":response.get("error")}) + "\n")
        self.commands.flush()
        if "error" in response:
            raise RuntimeError(method + ": " + json.dumps(response["error"]))
        return response.get("result", {})

    def pump(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                self.read(max(0.001, deadline - time.monotonic()))
            except TimeoutError:
                break

    def evaluate(self, expression, session, timeout=15):
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
            "awaitPromise": True, "userGesture": True,
        }, session, timeout)
        if result.get("exceptionDetails"):
            raise RuntimeError("JavaScript: " + json.dumps(result["exceptionDetails"]))
        return result.get("result", {}).get("value")

    def close(self):
        self.log.close()
        self.commands.close()
        self.ws.close()


def parse_proc_stat(text):
    end = text.rfind(")")
    name = text[text.index("(")+1:end]
    parts = text[end+2:].split()
    return {"name":name, "pgrp":int(parts[2]), "session":int(parts[3]),
            "ticks":int(parts[11])+int(parts[12]), "start_ticks":int(parts[19]),
            "rss_bytes":int(parts[21])*os.sysconf("SC_PAGE_SIZE")}


def process_snapshot(session_id):
    processes = {}
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            info = parse_proc_stat((directory / "stat").read_text())
            if info["session"] == session_id or info["name"] in ("gnome-shell", "Xwayland"):
                info["group"] = "browser" if info["session"] == session_id else "compositor"
                processes[directory.name] = info
        except (OSError, ValueError, IndexError):
            continue
    totals = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    return {"monotonic":time.monotonic(), "processes":processes,
            "system_total_ticks":sum(totals[:8]), "system_idle_ticks":totals[3]+totals[4]}


def cpu_delta(previous, current):
    elapsed = current["monotonic"] - previous["monotonic"]
    hz = os.sysconf("SC_CLK_TCK")
    groups = {"browser":0.0, "compositor":0.0}
    processes = []
    for pid, info in current["processes"].items():
        old = previous["processes"].get(pid)
        if old and old["start_ticks"] == info["start_ticks"]:
            ticks = max(0, info["ticks"] - old["ticks"])
            percent = 100*ticks/hz/elapsed if elapsed else 0
            groups[info["group"]] += percent
            processes.append({"pid":int(pid), "name":info["name"], "group":info["group"],
                              "cpu_percent_one_core":percent, "rss_bytes":info["rss_bytes"]})
    total = current["system_total_ticks"] - previous["system_total_ticks"]
    idle = current["system_idle_ticks"] - previous["system_idle_ticks"]
    return {"seconds":elapsed, "cpu_percent_one_core":groups, "processes":processes,
            "system_busy_percent_all_cores":100*(total-idle)/total if total else None,
            "rss_note":"RSS is per process and shared pages are not deduplicated",
            "cpu_note":"Only PIDs present at both endpoints with matching start time count; short-lived processes may be missed"}


def range_bounds(header, size):
    if header is None:
        return 0, size-1, False
    if not header.startswith("bytes=") or "," in header:
        raise ValueError("unsupported byte range")
    first, last = header[6:].split("-", 1)
    if not first:
        count = int(last)
        if count <= 0:
            raise ValueError("empty suffix range")
        start, end = max(0, size-count), size-1
    else:
        start = int(first)
        end = min(int(last), size-1) if last else size-1
    if start < 0 or start >= size or end < start:
        raise ValueError("unsatisfiable byte range")
    return start, end, True


def make_handler(media_dir, player, requests):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_HEAD(self):
            self.respond(False)

        def do_GET(self):
            self.respond(True)

        def respond(self, send_body):
            route = urllib.parse.urlsplit(self.path).path
            record = {"utc":utc(),"method":self.command,"path":route,"range":self.headers.get("Range"),
                      "user_agent":self.headers.get("User-Agent"),"status":None}
            requests.append(record)
            if route in ("/", "/player.html"):
                path, mime = player, "text/html; charset=utf-8"
            elif route.startswith("/media/") and route[7:] in CASES.values():
                path = media_dir / route[7:]
                mime = "video/webm" if path.suffix == ".webm" else "video/mp4"
            else:
                record["status"] = 404
                self.send_error(404)
                return
            if not path.is_file():
                record["status"] = 404
                self.send_error(404)
                return
            size = path.stat().st_size
            try:
                start, end, partial = range_bounds(self.headers.get("Range"), size)
            except (ValueError, TypeError):
                record["status"] = 416
                self.send_response(416)
                self.send_header("Content-Range", "bytes */" + str(size))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206 if partial else 200
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(end-start+1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            if partial:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            record.update(status=status, bytes=end-start+1)
            if send_body:
                try:
                    with path.open("rb") as stream:
                        stream.seek(start)
                        remaining = end-start+1
                        while remaining:
                            block = stream.read(min(65536, remaining))
                            if not block:
                                break
                            self.wfile.write(block)
                            remaining -= len(block)
                except (BrokenPipeError, ConnectionResetError):
                    pass
    return Handler


def h264_codec(path):
    with path.open("rb") as stream:
        data = stream.read(1024*1024)
    position = data.find(b"avcC")
    while position >= 0:
        if position >= 4 and position+8 <= len(data):
            size = int.from_bytes(data[position-4:position], "big")
            if size >= 12 and data[position+4] == 1:
                return "avc1." + data[position+5:position+8].hex().upper()
        position = data.find(b"avcC", position+4)
    raise ValueError("cannot read avcC in first MiB; specify --mse-codec")


def collect_players(events):
    players = {}
    for event in events:
        if not event.get("method", "").startswith("Media."):
            continue
        method, params = event["method"], event.get("params", {})
        identity = params.get("playerId")
        if identity:
            player = players.setdefault(identity, {"properties":{}, "errors":[], "messages":[], "events":[]})
            if method == "Media.playerPropertiesChanged":
                for prop in params.get("properties", []):
                    player["properties"][prop["name"]] = prop.get("value")
            elif method == "Media.playerErrorsRaised":
                player["errors"].extend(params.get("errors", []))
            elif method == "Media.playerMessagesLogged":
                player["messages"].extend(params.get("messages", []))
            elif method == "Media.playerEventsAdded":
                player["events"].extend(params.get("events", []))
    return players


def decoder_evidence(players, frames, mode):
    evidence = []
    for identity, player in players.items():
        props = player["properties"]
        decoder_names = {key:value for key, value in props.items()
                         if "video" in key.lower() and "decoder" in key.lower() and "name" in key.lower()}
        platform = {key:value for key, value in props.items() if "platformvideodecoder" in key.lower()}
        names = " ".join(str(value) for value in decoder_names.values())
        platform_false = any(str(value).lower() == "false" for value in platform.values())
        v4l2 = "v4l2" in names.lower() and not platform_false
        software = any(name in names for name in ("FFmpegVideoDecoder", "VpxVideoDecoder", "VPXVideoDecoder", "Dav1dVideoDecoder"))
        evidence.append({"player":identity, "decoder_names":decoder_names,
                         "platform_properties":platform, "v4l2_named":v4l2,
                         "software_named":software})
    confirmed = frames > 0 and any(item["v4l2_named"] if mode == "hardware" else item["software_named"] for item in evidence)
    return {"status":"confirmed" if confirmed else "not_proven", "mode":mode,
            "frames":frames, "players":evidence,
            "rule":"Hardware needs a named V4L2 decoder and presented frames; Mojo wrapper, GPU feature list or low CPU alone is insufficient"}


def browser_command(args, profile, mode):
    if profile.exists() or profile.is_symlink():
        raise ValueError("browser validation requires a new empty test profile")
    # The test profile never contains user credentials. Basic avoids a desktop
    # keyring prompt blocking unattended startup; it is not a product setting.
    base = [args.browser, "--user-data-dir=" + str(profile),
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=" + str(args.cdp_port),
            "--password-store=basic", "--no-proxy-server", "--enable-automation", "--no-first-run", "--no-default-browser-check",
            "--autoplay-policy=no-user-gesture-required", "--window-size=1280,900"]
    if os.geteuid() == 0:
        base.append("--no-sandbox")
    reserved = ("--user-data-dir", "--remote-debugging-port", "--remote-debugging-address", "--remote-debugging-pipe")
    for value in args.browser_arg + args.hardware_arg + args.software_arg:
        if any(value == name or value.startswith(name + "=") for name in reserved):
            raise ValueError("test ownership option cannot be overridden: " + value)
        if value.startswith("--password-store") and value != "--password-store=basic":
            raise ValueError("test profile uses only the basic password store")
    # Accept the previous explicit test flag without adding a duplicate.
    base.extend(value for value in args.browser_arg if value != "--password-store=basic")
    extra = args.hardware_arg if mode == "hardware" else ["--disable-accelerated-video-decode", *args.software_arg]
    base.extend(value for value in extra if value != "--password-store=basic")
    base.append("about:blank")
    return base


def port_free(port):
    # A finished debugger may leave TIME_WAIT sockets. Test for a listener,
    # not bind availability; ownership is separately verified after connect.
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def stop_owned_browser(process):
    """Signal only the new session created by this Popen(start_new_session=True)."""
    for sig, timeout in ((signal.SIGTERM, 5), (signal.SIGKILL, 3)):
        members = process_snapshot(process.pid)["processes"]
        own = [info for info in members.values() if info["session"] == process.pid]
        if not own:
            break
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None and not any(
                    info["session"] == process.pid for info in process_snapshot(process.pid)["processes"].values()):
                return
            time.sleep(0.1)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def direct_urlopen(request, timeout):
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout)


def http_health(port, player):
    request = urllib.request.Request("http://127.0.0.1:%d/player.html" % port,
                                     headers={"User-Agent":"Y4-harness-health"})
    started = time.monotonic()
    with direct_urlopen(request, timeout=3) as response:
        content = response.read()
        result = {"status":response.status,"bytes":len(content),"elapsed":time.monotonic()-started,
                  "sha256":hashlib.sha256(content).hexdigest()}
    expected = hashlib.sha256(player.read_bytes()).hexdigest()
    if result["status"] != 200 or result["sha256"] != expected:
        raise RuntimeError("local HTTP player self-check failed: " + json.dumps(result))
    return result


def wait_debugger(process, port, timeout=20):
    endpoint = "http://127.0.0.1:%d/json/version" % port
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("test browser exited before CDP became ready")
        try:
            with direct_urlopen(endpoint, timeout=1) as response:
                return json.load(response)
        except (OSError, ValueError):
            time.sleep(0.2)
    raise TimeoutError("test browser CDP did not become ready")


def run_case(cdp, session, process, name, mode, args, output):
    started = time.monotonic()
    index = len(cdp.events)
    case = {"name":name, "mode":mode, "started_utc":utc(), "samples":[], "checks":{},
            "screenshots":[], "event_start_index":index}
    cpu_previous = process_snapshot(process.pid)
    samples_log = (output / (name + "-samples.jsonl")).open("w")

    def sample():
        nonlocal cpu_previous
        cdp.check_boot()
        video = cdp.evaluate("window.sample()", session)
        cpu = process_snapshot(process.pid)
        entry = {"utc":utc(), "elapsed":time.monotonic()-started, "video":video,
                 "cpu":cpu_delta(cpu_previous, cpu)}
        cpu_previous = cpu
        case["samples"].append(entry)
        samples_log.write(json.dumps(entry, allow_nan=False) + "\n")
        samples_log.flush()
        return video

    def screenshot(label):
        result = cdp.call("Page.captureScreenshot", {"format":"png", "fromSurface":True}, session)
        path = output / (name + "-" + label + ".png")
        path.write_bytes(base64.b64decode(result["data"]))
        case["screenshots"].append(path.name)

    def wait_for(predicate, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = sample()
            if value.get("error") or value.get("failures"):
                raise RuntimeError("video error: " + json.dumps(value))
            if predicate(value):
                return value
            cdp.pump(min(args.sample_interval, max(0.01, deadline-time.monotonic())))
        raise TimeoutError("video state deadline exceeded for " + name)

    try:
        mse = name.endswith("-mse")
        mime = ('video/mp4; codecs="' + args.mse_codec + '"') if mse else (
            "video/webm" if CASES[name].endswith(".webm") else "video/mp4")
        spec = {"name":name, "label":mode + " / " + name + " / 1080p60",
                "url":"/media/" + CASES[name], "mime":mime, "mse":mse}
        case["initial"] = cdp.evaluate("window.startCase(" + json.dumps(spec) + ")", session, timeout=args.timeout)
        playing = wait_for(lambda value: value.get("currentTime", 0) >= 2 and value.get("presentedFrameCallbacks", 0) > 0, args.timeout)
        case["checks"]["started_and_presented_frames"] = True
        screenshot("playing")
        paused = cdp.evaluate("window.video.pause(); window.sample()", session)
        cdp.pump(1)
        paused_after = sample()
        case["checks"]["pause_stable"] = bool(paused_after.get("paused") and abs(paused_after["currentTime"]-paused["currentTime"]) < 0.1)
        duration = playing.get("duration")
        target = min(6.0, duration/2) if isinstance(duration, (int, float)) and duration > 0 else 6.0
        sought = cdp.evaluate("window.seekTo(" + str(target) + ")", session, timeout=10)
        case["checks"]["seek_completed"] = abs(sought["currentTime"]-target) < 0.35
        cdp.evaluate("window.video.play().then(()=>window.sample())", session)
        fullscreen = cdp.evaluate("window.toggleFullscreen(true)", session)
        case["checks"]["fullscreen_entered"] = bool(fullscreen.get("ok"))
        cdp.pump(0.5)
        screenshot("fullscreen")
        restored = cdp.evaluate("window.toggleFullscreen(false)", session)
        case["checks"]["fullscreen_exited"] = bool(restored.get("ok"))
        final = wait_for(lambda value: value.get("ended", False), args.timeout)
        case["checks"]["ended"] = True
        case["final_video"] = final
        screenshot("ended")
        cdp.pump(0.5)
    except Exception as error:
        case["error"] = str(error)
        case["traceback"] = traceback.format_exc()
        try:
            case["final_video"] = sample()
            screenshot("failure")
        except Exception:
            pass
    finally:
        samples_log.close()
    case["elapsed_seconds"] = time.monotonic()-started
    case["event_end_index"] = len(cdp.events)
    case["players"] = collect_players([event for event in cdp.events[index:] if event.get("sessionId") == session])
    frames = case.get("final_video", {}).get("presentedFrameCallbacks", 0)
    case["checks"]["media_errors_absent"] = not any(player["errors"] for player in case["players"].values())
    case["decoder_evidence"] = decoder_evidence(case["players"], frames, mode)
    case["playback_passed"] = not case.get("error") and all(case["checks"].values()) and len(case["checks"]) == 7
    case["status"] = ("passed" if case["playback_passed"] and case["decoder_evidence"]["status"] == "confirmed"
                      else "evidence_incomplete" if case["playback_passed"] else "failed")
    dump(output / (name + "-result.json"), case)
    return case


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="new empty output directory; never reuse")
    parser.add_argument("--browser", default="/usr/bin/chromium")
    parser.add_argument("--mode", choices=("both","hardware","software"), default="both")
    parser.add_argument("--cases", default="h264,vp9,vp8,h264-mse")
    parser.add_argument("--browser-arg", action="append", default=[])
    parser.add_argument("--hardware-arg", action="append", default=[])
    parser.add_argument("--software-arg", action="append", default=[])
    parser.add_argument("--cdp-port", type=int, default=9223)
    parser.add_argument("--http-port", type=int, default=8765)
    parser.add_argument("--sample-interval", type=float, default=1)
    parser.add_argument("--timeout", type=float, default=35)
    parser.add_argument("--mse-codec", help="default: extract avc1 profile/compatibility/level from h264-mse.mp4")
    parser.add_argument("--runtime-dir", default="/run/user/0")
    parser.add_argument("--wayland-display", default="wayland-0")
    return parser.parse_args()


def main():
    args = arguments()
    args.media_dir = args.media_dir.resolve()
    args.output = args.output.resolve()
    selected = args.cases.split(",")
    if not selected or any(name not in CASES for name in selected) or len(set(selected)) != len(selected):
        raise ValueError("cases must be a unique comma-separated subset of " + ",".join(CASES))
    if args.sample_interval <= 0 or args.timeout <= 0:
        raise ValueError("timeouts and sample interval must be positive")
    for name in selected:
        if not (args.media_dir / CASES[name]).is_file():
            raise ValueError("missing test media: " + CASES[name])
    if "h264-mse" in selected and not args.mse_codec:
        args.mse_codec = h264_codec(args.media_dir / CASES["h264-mse"])
    if not port_free(args.cdp_port):
        raise ValueError("CDP port is occupied; refusing to attach to an existing browser")
    if args.cdp_port == args.http_port:
        raise ValueError("CDP and HTTP ports must differ")
    args.output.mkdir(parents=True, exist_ok=False)
    expected_boot = boot_id()
    report = {"schema":1, "started_utc":utc(), "boot_id":expected_boot,
              "modes":{}, "media":{}, "limitations":[
                  "Virtual and physical display acceptance must be recorded separately.",
                  "CPU results exclude processes that start and exit between samples.",
                  "Video frame callbacks and decoder names do not establish DMA-BUF zero-copy.",
                  "Playback is muted; no audio/video synchronization threshold is measured.",
                  "MSE uses one fragmented MP4 appended in chunks; it does not test adaptive bitrate switching.",
                  "The root test uses no-sandbox; it does not validate ordinary-user sandbox compatibility.",
                  "The new empty test profile uses password-store=basic only to avoid keyring prompts; product configuration is unchanged."]}
    for name in selected:
        path = args.media_dir / CASES[name]
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream,"sha256").hexdigest()
        report["media"][name] = {"path":str(path), "bytes":path.stat().st_size,"sha256":digest}
    dump(args.output / "report.json", report)
    requests = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", args.http_port),
              make_handler(args.media_dir, Path(__file__).with_name("player.html"), requests))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    modes = ("hardware","software") if args.mode == "both" else (args.mode,)
    try:
        report["http_health_before_browser"] = http_health(args.http_port, Path(__file__).with_name("player.html"))
        dump(args.output / "report.json", report)
        for mode in modes:
            if boot_id() != expected_boot:
                raise RuntimeError("boot ID changed before next mode")
            if not port_free(args.cdp_port):
                raise RuntimeError("CDP port is occupied before next mode")
            output = args.output / mode
            output.mkdir()
            profile = output / "profile"
            command = browser_command(args, profile, mode)
            env = dict(os.environ, XDG_RUNTIME_DIR=args.runtime_dir, WAYLAND_DISPLAY=args.wayland_display,
                       DBUS_SESSION_BUS_ADDRESS="unix:path=" + args.runtime_dir + "/bus")
            mode_report = {"command":command, "cases":[]}
            report["modes"][mode] = mode_report
            cdp = None
            cdp_owned = False
            with (output / "browser.log").open("wb") as log:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True, env=env)
                mode_report["owned_pid"] = process.pid
                try:
                    version = wait_debugger(process, args.cdp_port)
                    mode_report["debugger_version"] = version
                    cdp = CDP(version["webSocketDebuggerUrl"], output / "cdp-events.jsonl", expected_boot)
                    actual_arguments = cdp.call("Browser.getBrowserCommandLine").get("arguments", [])
                    if "--user-data-dir=" + str(profile) not in actual_arguments:
                        raise RuntimeError("CDP browser does not own the newly created test profile")
                    cdp_owned = True
                    mode_report["actual_browser_arguments"] = actual_arguments
                    mode_report["system_info"] = cdp.call("SystemInfo.getInfo")
                    for name in selected:
                        target = cdp.call("Target.createTarget", {"url":"about:blank"})["targetId"]
                        session = cdp.call("Target.attachToTarget", {"targetId":target, "flatten":True})["sessionId"]
                        try:
                            for domain in ("Page","Runtime","Media","Network"):
                                cdp.call(domain + ".enable", session=session)
                            cdp.call("Network.setCacheDisabled", {"cacheDisabled":True}, session)
                            cdp.call("Page.setLifecycleEventsEnabled", {"enabled":True}, session)
                            try:
                                cdp.call("Page.navigate", {"url":"http://127.0.0.1:%d/player.html" % args.http_port}, session, timeout=args.timeout)
                            except Exception:
                                try:
                                    mode_report["navigation_diagnostic"] = cdp.evaluate("({url:location.href,readyState:document.readyState,title:document.title})", session, timeout=3)
                                except Exception as diagnostic_error:
                                    mode_report["navigation_diagnostic"] = {"error":str(diagnostic_error)}
                                mode_report["http_health_after_navigation_error"] = http_health(args.http_port, Path(__file__).with_name("player.html"))
                                raise
                            deadline = time.monotonic()+10
                            while time.monotonic() < deadline:
                                if cdp.evaluate("typeof window.startCase === 'function'", session):
                                    break
                                cdp.pump(0.2)
                            else:
                                raise TimeoutError("player page did not initialize")
                            graphics = cdp.evaluate("""(()=>{
                              const c=document.createElement('canvas'),g=c.getContext('webgl2')||c.getContext('webgl');
                              if(!g)return {webgl:false};const e=g.getExtension('WEBGL_debug_renderer_info');
                              return {webgl:true,vendor:g.getParameter(e?e.UNMASKED_VENDOR_WEBGL:g.VENDOR),
                              renderer:g.getParameter(e?e.UNMASKED_RENDERER_WEBGL:g.RENDERER),version:g.getParameter(g.VERSION)};})()""", session)
                            case = run_case(cdp, session, process, name, mode, args, output)
                            case["graphics"] = graphics
                            feature = mode_report["system_info"].get("gpu",{}).get("featureStatus",{}).get("gpu_compositing")
                            case["gpu_evidence"] = {
                                "status":"confirmed" if "mali" in str(graphics.get("renderer","")).lower() and feature == "enabled" else "not_proven",
                                "webgl_renderer":graphics.get("renderer"),"gpu_compositing":feature,
                                "rule":"Mali WebGL renderer plus GPU compositing enabled; screenshot still requires visual review"}
                            if case["status"] == "passed" and case["gpu_evidence"]["status"] != "confirmed":
                                case["status"] = "evidence_incomplete"
                            dump(output / (name + "-result.json"), case)
                            mode_report["cases"].append(case)
                            dump(args.output / "report.json", report)
                            print(json.dumps({"mode":mode,"case":name,"status":case["status"],"decoder_evidence":case["decoder_evidence"]}),flush=True)
                        finally:
                            cdp.call("Target.closeTarget", {"targetId":target})
                        if case.get("error"):
                            break
                finally:
                    if cdp:
                        if cdp_owned:
                            try:
                                cdp.call("Browser.close", timeout=2)
                            except Exception:
                                pass
                        cdp.close()
                    stop_owned_browser(process)
                    mode_report["returncode"] = process.poll()
    except Exception as error:
        report["harness_error"] = str(error)
        report["traceback"] = traceback.format_exc()
    finally:
        httpd.shutdown()
        httpd.server_close()
        report["http_requests"] = requests
        report["finished_utc"] = utc()
        report["final_boot_id"] = boot_id()
        cases = [case for mode in report["modes"].values() for case in mode["cases"]]
        report["complete"] = len(cases) == len(selected)*len(modes) and not report.get("harness_error")
        report["passed"] = report["complete"] and all(case["status"] == "passed" for case in cases)
        dump(args.output / "report.json", report)
    if report.get("harness_error"):
        return 1
    if not report["complete"] or any(case["status"] == "failed" for case in cases):
        return 2
    return 0 if report["passed"] else 3


if __name__ == "__main__":
    sys.exit(main())
