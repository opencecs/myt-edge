#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""Bounded Debian board initialization; only fixed native helpers perform IO."""
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import resource
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

RUN = Path("/run/y4-board")
STATE = Path("/var/lib/y4-board")
ASSETS = Path("/usr/share/y4-board")
HELPER = "/usr/libexec/y4-board/y4-board-boot-helper"
MCU = "/usr/libexec/y4-board/y4-mcu-maintain"
NIC = "/usr/libexec/y4-board/y4-nic-factory"
HOST = Path("/etc/y4-board/host-version")
BOND_PROFILE = Path("/etc/NetworkManager/system-connections/y4-bond.nmconnection")
LIMIT = 5
BUDGET_SECONDS = 300.0
stopping = False

class BootError(Exception):
    def __init__(self, code, phase, detail):
        self.code = code if -4095 <= code < 0 else -errno.EIO
        self.phase = phase
        self.detail = detail
        super().__init__(detail)

def stop(_signum, _frame):
    global stopping
    stopping = True

def private_dir(path):
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid or stat.S_IMODE(s.st_mode) != 0o700:
        raise BootError(-errno.EPERM, "directory", str(path))

def trusted_dir(path, mode=None):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    s = os.fstat(fd)
    if s.st_uid or s.st_mode & 0o022 or (mode is not None and stat.S_IMODE(s.st_mode) != mode):
        os.close(fd)
        raise BootError(-errno.EPERM, "directory", str(path))
    return fd

def read_data(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid or s.st_nlink != 1 or s.st_mode & 0o022 or s.st_size > maximum:
            raise BootError(-errno.EPERM, "asset", str(path))
        chunks = []
        total = 0
        while True:
            data = os.read(fd, min(65536, maximum + 1 - total))
            if not data:
                break
            total += len(data)
            if total > maximum:
                raise BootError(-errno.EFBIG, "asset", str(path))
            chunks.append(data)
        end = os.fstat(fd)
        if (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns) != (end.st_dev, end.st_ino, end.st_size, end.st_mtime_ns, end.st_ctime_ns):
            raise BootError(-errno.ESTALE, "asset", str(path))
        return b"".join(chunks)
    finally:
        os.close(fd)

def pairs(items):
    out = {}
    for key, value in items:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out

def decode_json(raw):
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                       parse_float=lambda _: (_ for _ in ()).throw(ValueError("float")),
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value

def read_json(path, maximum=65536):
    return decode_json(read_data(path, maximum))

def atomic(path, data, mode=0o600):
    directory = trusted_dir(path.parent)
    temporary = "." + path.name + "-" + str(os.getpid()) + "-" + os.urandom(8).hex()
    fd = -1
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=directory)
        at = 0
        while at < len(data):
            n = os.write(fd, data[at:])
            if n <= 0:
                raise OSError(errno.EIO, "short write")
            at += n
        os.fchmod(fd, mode)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.rename(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)

def atomic_json(path, value):
    atomic(path, (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())

def boot_id():
    value = read_data(Path("/proc/sys/kernel/random/boot_id"), 64).decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value):
        raise BootError(-errno.EBADMSG, "boot_identity", "invalid kernel boot ID")
    return value

def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("integer range")
    return value

def lock_file(path, create):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    s = os.fstat(fd)
    if not stat.S_ISREG(s.st_mode) or s.st_uid or s.st_nlink != 1 or stat.S_IMODE(s.st_mode) != 0o600:
        os.close(fd)
        raise BootError(-errno.EPERM, "lock", str(path))
    return fd

def lock_free(path, missing_ok=True):
    try:
        fd = lock_file(path, False)
    except FileNotFoundError:
        return missing_ok
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    finally:
        os.close(fd)

def birth(pid):
    try:
        raw = Path("/proc/" + str(pid) + "/stat").read_text()
        return int(raw[raw.rfind(") ") + 2:].split()[19])
    except (FileNotFoundError, ProcessLookupError):
        return None

def executable(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid or s.st_nlink != 1 or s.st_mode & 0o022 or not s.st_mode & 0o111:
            raise BootError(-errno.EPERM, "helper", path)
    finally:
        os.close(fd)

BARRIER_FIELDS = ("owner_pid", "owner_birth_ticks", "started_monotonic_ms")

def barrier_intent(raw):
    """Parse the versioned Y4NICWRITE1 intent marker published by
    y4-nic-factory before an irreversible PG write. Rejects anything the
    writer could not have produced, and verifies the recorded owner PID
    against its process birth ticks while that owner is still alive."""
    text = raw.decode("ascii")
    lines = text.split("\n")
    if lines[0] != "Y4NICWRITE1":
        raise ValueError("barrier version")
    fields = {}
    explained = False
    for line in lines[1:]:
        if not line:
            continue
        if explained:
            raise ValueError("barrier trailer")
        key, sep, value = line.partition("=")
        if sep:
            if key not in BARRIER_FIELDS or key in fields or not value.isdigit():
                raise ValueError("barrier field")
            fields[key] = int(value)
        else:
            if not line.startswith("Irreversible PG attempt"):
                raise ValueError("barrier explanation")
            explained = True
    if len(fields) != len(BARRIER_FIELDS):
        raise ValueError("barrier fields incomplete")
    if fields["owner_pid"] < 2 or fields["owner_birth_ticks"] <= 0 or fields["started_monotonic_ms"] <= 0:
        raise ValueError("barrier field range")
    if fields["started_monotonic_ms"] > time.monotonic_ns() // 1000000:
        raise ValueError("barrier timestamp")
    live_birth = birth(fields["owner_pid"])
    if live_birth is not None and live_birth != fields["owner_birth_ticks"]:
        raise ValueError("barrier owner mismatch")
    return fields

def pending_barriers():
    pending = []
    parent = RUN / "nic"
    try:
        fd = trusted_dir(parent, 0o700)
        os.close(fd)
    except FileNotFoundError:
        return pending
    except (OSError, BootError):
        return ["nic-directory-untrusted"]
    for attempt in range(1, LIMIT + 1):
        directory = parent / ("attempt-%d" % attempt)
        try:
            fd = trusted_dir(directory, 0o700)
            os.close(fd)
        except FileNotFoundError:
            continue
        except (OSError, BootError):
            pending.append("attempt-%d-directory-untrusted" % attempt)
            continue
        marker = directory / "write-attempted"
        try:
            intent = read_data(marker, 512)
            if stat.S_IMODE(marker.lstat().st_mode) != 0o600:
                raise ValueError("untrusted write barrier mode")
            barrier_intent(intent)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, UnicodeError, BootError):
            pending.append("attempt-%d-write-barrier-untrusted" % attempt)
            continue
        complete = directory / "complete"
        try:
            result = read_data(complete, 64).decode("ascii")
            if stat.S_IMODE(complete.lstat().st_mode) != 0o600 or not re.fullmatch(r"status=(?:0|-[1-9][0-9]*)\n", result):
                raise ValueError("untrusted completion")
            integer(int(result[7:].strip()), -4095, 0)
        except (OSError, ValueError, UnicodeError, BootError):
            pending.append("attempt-%d-write-outcome-unknown" % attempt)
    return pending

def sequence(value):
    return integer(value, 0, (1 << 64) - 1)

def package_hash(path):
    return hashlib.sha256(read_data(path, 1049736)).hexdigest()

def select_package():
    directory = trusted_dir(ASSETS)
    os.close(directory)
    builtin = read_json(ASSETS / "mcu-builtin.json")
    if builtin.get("schema") != 1:
        raise ValueError("builtin schema")
    floor = sequence(builtin.get("sequence"))
    expected = builtin.get("sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("builtin hash")
    chosen = ASSETS / "mcu-package.bin"
    if package_hash(chosen) != expected:
        raise BootError(-errno.EBADMSG, "package", "builtin package hash mismatch")
    selection = "builtin"
    try:
        active = read_json(STATE / "active.json")
    except FileNotFoundError:
        active = None
    if active is not None:
        if active.get("schema") != 1:
            raise ValueError("active schema")
        active_sequence = sequence(active.get("sequence"))
        name = active.get("package")
        active_hash = active.get("sha256")
        if not isinstance(name, str) or not re.fullmatch(r"firmware-[0-9a-f]{32}\.bin", name):
            raise ValueError("active filename")
        if not isinstance(active_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", active_hash):
            raise ValueError("active hash")
        active_path = STATE / name
        if package_hash(active_path) != active_hash:
            raise BootError(-errno.EBADMSG, "package", "active package hash mismatch; no fallback")
        if active_sequence == floor and active_hash != expected:
            raise BootError(-errno.EBADMSG, "package", "conflicting packages at equal sequence")
        if active_sequence >= floor:
            chosen, expected, floor, selection = active_path, active_hash, active_sequence, "active"
    key = ASSETS / "mcu-public-key.bin"
    if len(read_data(key, 32)) != 32:
        raise ValueError("key length")
    host = read_data(HOST, 64).decode("ascii").strip()
    if not re.fullmatch(r"[A-Za-z0-9.+-]{1,63}", host):
        raise ValueError("host compatibility version")
    return chosen, key, host, floor, {"selection": selection, "sequence": floor, "sha256": expected}

class Initializer:
    def __init__(self, current_boot, board="Y4"):
        self.boot = current_boot
        self.board = board
        self.counter = {"schema": 1, "boot_id": current_boot, "attempts": 0, "phase": "idle",
                        "started_ns": time.monotonic_ns(), "history": []}
        self.state = {"schema": 1, "boot_id": current_boot, "attempt": 0, "max_attempts": LIMIT,
                      "phase": "checking", "status": -errno.EINPROGRESS, "completed": False,
                      "network_safe": False, "usable_mask": 0, "writers_settled": False,
                      "mcu_restored": None, "mcu_safe_for_network": None, "nic_restore_status": None}
        self.child = None
        self.deadline = time.monotonic() + BUDGET_SECONDS
        self.nic_restore_failed = False
        self.ownership_known = True

    def persist(self):
        atomic_json(RUN / "boot-counter.json", self.counter)

    def publish(self, phase=None, code=None, **extra):
        if phase is not None:
            self.state["phase"] = phase
        if code is not None:
            self.state["status"] = code
        self.state.update(extra)
        self.state["attempt"] = self.counter["attempts"]
        self.state["updated_ms"] = time.monotonic_ns() // 1000000
        atomic_json(RUN / "initialization.json", self.state)

    def restore(self):
        self.ownership_known = False
        try:
            value = read_json(RUN / "boot-counter.json")
        except FileNotFoundError:
            previous = list((RUN / "boot").glob("mcu-attempt-*.json")) + list((RUN / "nic").glob("attempt-*"))
            if previous:
                self.counter["phase"] = "unknown"
                self.persist()
                raise BootError(-errno.EOWNERDEAD, "counter", "missing counter with prior operation evidence")
            self.persist()
            self.ownership_known = True
            return "fresh"
        if value.get("schema") != 1 or value.get("boot_id") != self.boot:
            raise BootError(-errno.ESTALE, "counter", "counter boot/schema mismatch")
        attempts = integer(value.get("attempts"), 0, LIMIT)
        started = integer(value.get("started_ns"), 1, time.monotonic_ns())
        if value.get("phase") not in ("idle", "reserved", "running", "settled", "complete", "unknown"):
            raise ValueError("counter phase")
        history = value.get("history")
        if not isinstance(history, list) or len(history) > LIMIT:
            raise ValueError("counter history")
        if type(value.get("unknown_writer", False)) is not bool:
            raise ValueError("writer uncertainty flag")
        self.counter = value
        self.nic_restore_failed = bool(value.get("nic_restore_failed", False))
        if type(value.get("nic_restore_failed", False)) is not bool:
            raise ValueError("restoration flag")
        self.ownership_known = True
        self.state["attempt"] = attempts
        self.deadline = started / 1e9 + BUDGET_SECONDS
        if value["phase"] == "complete":
            return "complete"
        if value["phase"] in ("reserved", "running", "unknown"):
            return "interrupted"
        return "resume"

    def start(self, stage, args):
        if self.child is not None:
            raise BootError(-errno.EBUSY, stage, "child still owned")
        executable(args[0])
        name = "attempt-%d-%s-%d.log" % (self.counter["attempts"], stage, time.monotonic_ns())
        log_path = RUN / "boot" / name
        log = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        rd, wr = os.pipe2(os.O_CLOEXEC)
        parent = os.getpid()
        try:
            pid = os.fork()
        except BaseException:
            os.close(rd)
            os.close(wr)
            os.close(log)
            raise
        if pid == 0:
            try:
                os.close(wr)
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                libc = ctypes.CDLL(None, use_errno=True)
                if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != parent:
                    os._exit(125)
                if os.read(rd, 1) != b"1":
                    os._exit(125)
                os.close(rd)
                null = os.open("/dev/null", os.O_RDONLY)
                os.dup2(null, 0)
                os.dup2(log, 1)
                os.dup2(log, 2)
                high = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
                if high == resource.RLIM_INFINITY:
                    high = 1048576
                os.closerange(3, int(high))
                os.execve(args[0], args, {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"})
            except BaseException:
                os._exit(126)
        os.close(rd)
        os.close(log)
        child_birth = birth(pid)
        self.child = {"pid": pid, "birth": child_birth, "stage": stage, "log": str(log_path),
                      "term_sent": False, "overdue": False, "normal_exit": False}
        self.counter.update(phase="running", child_pid=pid, child_birth=child_birth, child_stage=stage, child_log=name)
        try:
            self.persist()  # The child cannot exec until this intent is durable.
            if child_birth is None:
                raise BootError(-errno.ECHILD, stage, "child birth unavailable")
            os.write(wr, b"1")
        finally:
            os.close(wr)
        return self.child

    def wait(self, child, seconds, global_budget=True):
        deadline = time.monotonic() + seconds
        if global_budget:
            deadline = min(deadline, self.deadline)
        while True:
            try:
                got, raw = os.waitpid(child["pid"], os.WNOHANG)
            except InterruptedError:
                continue
            except ChildProcessError:
                self.ownership_known = False
                self.counter["phase"] = "unknown"
                self.persist()
                self.publish("ownership_unknown", -errno.ECHILD)
                # Do not signal a PID after direct-child ownership was lost.
                while birth(child["pid"]) == child["birth"]:
                    time.sleep(0.1)
                child["raw_status"] = None
                child["normal_exit"] = False
                self.child = None
                return -errno.ECHILD
            if got == child["pid"]:
                child["raw_status"] = raw
                child["normal_exit"] = os.WIFEXITED(raw)
                self.child = None
                return 0 if os.WIFEXITED(raw) and os.WEXITSTATUS(raw) == 0 else -errno.EIO
            expired = time.monotonic() >= deadline
            if (expired or stopping) and not child["term_sent"]:
                child["term_sent"] = True
                try:
                    os.kill(child["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass  # Actual waitpid, not kill outcome, establishes exit.
                except OSError as error:
                    self.state["cancel_signal_error"] = -(error.errno or errno.EIO)
                child["overdue"] = expired
                self.publish("cancelled_pending" if stopping else "overdue_pending",
                             -errno.ECANCELED if stopping else -errno.ETIMEDOUT,
                             pending_stage=child["stage"], pending_pid=child["pid"])
            # Timeout is not an exit. Keep owning the actual child and locks,
            # while Type=simple allows unrelated Debian startup/login to proceed.
            time.sleep(0.1)

    def call(self, stage, args, seconds, global_budget=True):
        if stopping:
            raise BootError(-errno.ECANCELED, stage, "initializer stopping")
        if global_budget and time.monotonic() >= self.deadline:
            raise BootError(-errno.ETIMEDOUT, stage, "boot budget exhausted before action")
        child = self.start(stage, args)
        code = self.wait(child, seconds, global_budget)
        return code, child

    def display(self, phase, code):
        if self.board == "Y12":
            return 0
        result, child = self.call("display-" + phase, [HELPER, "--display", phase,
                                  str(self.counter["attempts"]), str(code)], 2.0, False)
        self.state["display_status"] = result
        if child["overdue"] or not child["normal_exit"]:
            raise BootError(-errno.ETIMEDOUT, "display", "display worker did not finish normally")
        return result

    def interrupted(self):
        pid = self.counter.get("child_pid")
        recorded_birth = self.counter.get("child_birth")
        self.ownership_known = False
        self.publish("interrupted_unknown", -errno.EOWNERDEAD, pending_stage=self.counter.get("child_stage", "starting"))
        if type(pid) is int and pid > 1 and type(recorded_birth) is int:
            while birth(pid) == recorded_birth:
                # This is a prior owner's child. Never kill/reap a foreign PID.
                time.sleep(0.1)
        while not lock_free(RUN / "mcu.lock") or not lock_free(RUN / "nic.lock"):
            time.sleep(0.1)
        stage = self.counter.get("child_stage", "")
        auxiliary = stage in ("clear-uid", "publish-uid", "inspect") or stage.startswith("display-")
        if auxiliary and not self.counter.get("unknown_writer", False) and not pending_barriers():
            # These fixed helpers cannot enter ROM, launch PG or unbind a NIC.
            # Resume only after the recorded old process and its leases ended.
            self.ownership_known = True
            self.counter.update(phase="settled" if self.counter["attempts"] else "idle",
                                child_pid=0, child_birth=None)
            self.persist()
            if stopping:
                return self.finish(-errno.ECANCELED)
            return self.run()
        # No automatic retry after an interrupted ROM/PG writer.
        self.counter["phase"] = "unknown"
        self.persist()
        return self.finish(-errno.EOWNERDEAD, unknown=True)

    def inspect(self):
        code, child = self.call("inspect", [NIC, "--inspect"], 5.0, False)
        if code or child["overdue"] or not child["normal_exit"]:
            raise BootError(code or -errno.ETIMEDOUT, "inspection", "native inspection incomplete")
        result = last_json(Path(child["log"]), {"schema", "status", "driver_mask", "usable_mask"})
        report = nic_inspection(result)
        self.state.update(report)
        return report["observed_usable_mask"]

    def bond_profile(self, parent, usable):
        directory = parent / "system-connections"
        mac = self.state.get("observed_bond_mac")
        if usable & 6 == 6 and mac is not None:
            try:
                profile = read_data(BOND_PROFILE, 65536)
            except FileNotFoundError:
                profile = b""
            marker = b"\n[ethernet]\ncloned-mac-address=stable\n"
            if (profile.count(marker) == 1
                    and b"\nuuid=948fe2c1-4018-4acf-b25b-ecedb97c1000\n" in profile
                    and b"\ninterface-name=bond10g\n" in profile):
                directory.mkdir(mode=0o755, exist_ok=True)
                # An explicit profile setting also restores the MAC when the
                # kernel resets it after detaching the last member.
                updated = marker.replace(b"=stable", b"=" + mac.encode("ascii"))
                atomic(directory / BOND_PROFILE.name, profile.replace(marker, updated, 1))
                return mac
        # Keep the shipped stable profile for an incomplete or custom NIC cohort.
        try:
            fd = trusted_dir(directory)
        except FileNotFoundError:
            return None
        try:
            try:
                os.unlink(BOND_PROFILE.name, dir_fd=fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(fd)
        return None

    def result_config(self, usable):
        parent = Path("/run/NetworkManager")
        for directory in (parent, parent / "conf.d"):
            try:
                directory.mkdir(mode=0o755)
            except FileExistsError:
                pass
            fd = trusted_dir(directory)
            os.close(fd)
        prefix = self.board.lower()
        names = (prefix + "lan", prefix + "sfp1", prefix + "sfp2")
        text = "# Generated from settled current-boot native inspection.\n"
        for i, name in enumerate(names):
            if not usable & (1 << i):
                text += ("\n[device-y4-board-result-%s]\nmatch-device=interface-name:%s\nmanaged=0\n" % (name, name))
        atomic(parent / "conf.d/31-y4-board-result.conf", text.encode(), 0o644)
        self.state["bond_mac"] = None
        try:
            self.state["bond_mac"] = self.bond_profile(parent, usable)
        except (BootError, OSError) as error:
            # Stock stable identity and usable physical ports stay available.
            self.state["bond_profile_error"] = str(error)

    def finish(self, code, unknown=False):
        if unknown or not self.ownership_known:
            self.counter["unknown_writer"] = True
            self.persist()
        self.publish("finalizing", code)
        usable = 0
        if not stopping:
            try:
                usable = self.inspect()
            except (BootError, OSError, ValueError) as error:
                self.state["inspection_error"] = str(error)
        if code == 0 and self.state.get("nic_mode") == "inspect-only" and not usable:
            code = -errno.ENODEV
            self.counter["history"][-1]["status"] = code
        while not lock_free(RUN / "mcu.lock") or not lock_free(RUN / "nic.lock"):
            self.publish("writer_lock_pending", -errno.EINPROGRESS)
            time.sleep(0.1)
        barriers = pending_barriers()
        if barriers:
            self.state["pending_barrier"] = barriers
        known = self.ownership_known and not unknown and not barriers and not self.counter.get("unknown_writer", False)
        # Restored normal NICs remain usable after a known terminal MCU failure.
        # A live/unknown writer or explicit NIC restoration failure never passes.
        safe = (known and not self.nic_restore_failed and usable != 0 and not stopping and
                self.state.get("pgdrv_loaded") is False and self.state.get("pgdrv_status") == 0)
        self.result_config(usable if safe else 0)
        if not stopping:
            try:
                self.display("ready" if code == 0 else "failed", code)
            except (BootError, OSError, ValueError) as error:
                self.state["display_error"] = str(error)
                if not self.ownership_known:
                    safe = False
        self.counter.update(phase=("settled" if stopping else "complete") if known else "unknown",
                            child_pid=0, child_birth=None,
                            status=code, nic_restore_failed=self.nic_restore_failed)
        self.persist()
        # A collaborative cancellation is not a terminal result: the boot
        # budget survives, and a restarted service re-checks the settled
        # devices and finishes the release. Only unknown writers stay
        # frozen for this boot.
        self.publish("ready" if code == 0 and not stopping else "cancelled" if stopping else "failed",
                     code, completed=known and not stopping, network_safe=safe, usable_mask=usable,
                     writers_settled=known and not stopping,
                     deadline_exceeded=time.monotonic() >= self.deadline)
        return 0

    def run(self):
        state = self.restore()
        barriers = pending_barriers()
        if barriers:
            self.ownership_known = False
            self.state["pending_barrier"] = barriers
            self.counter["phase"] = "unknown"
            self.persist()
            return self.finish(-errno.EOWNERDEAD, unknown=True)
        if state == "complete":
            try:
                previous = read_json(RUN / "initialization.json")
                if previous.get("schema") == 1 and previous.get("boot_id") == self.boot and previous.get("completed") is True:
                    return 0
            except (OSError, ValueError, BootError):
                pass
            terminal = integer(self.counter.get("status"), -4095, 0)
            return self.finish(terminal)
        if state == "interrupted":
            return self.interrupted()
        if self.counter.get("mcu_retry_blocked", False) is True:
            self.state["mcu_retry_blocked"] = True
            return self.finish(integer(self.counter["status"], -4095, -1))
        self.publish("checking", -errno.EINPROGRESS)
        for path in (HELPER, MCU, NIC):
            executable(path)
        # Revoke any earlier UID before selecting/maintaining a package.
        code, child = self.call("clear-uid", [HELPER, "--clear-uid"], 2.0, False)
        if code or child["overdue"] or not child["normal_exit"]:
            raise BootError(code or -errno.ETIMEDOUT, "uid", "UID revocation failed")
        try:
            package, key, host, floor, selection = select_package()
        except (OSError, ValueError, UnicodeError, BootError) as error:
            self.counter["attempts"] = min(LIMIT, self.counter["attempts"] + 1)
            self.counter["phase"] = "settled"
            self.persist()
            self.state["package_error"] = str(error)
            return self.finish(error.code if isinstance(error, BootError) else -errno.EBADMSG)
        self.state["firmware_selection"] = selection
        result = -errno.EIO
        while self.counter["attempts"] < LIMIT and not stopping and time.monotonic() < self.deadline:
            barriers = pending_barriers()
            if barriers:
                self.ownership_known = False
                self.state["pending_barrier"] = barriers
                result = -errno.EOWNERDEAD
                break
            self.counter["attempts"] += 1
            # state_key: `key` is the trusted MCU key path from
            # select_package(); shadowing it here turned --key into the
            # literal "mcu_status" and failed every boot (found on hardware
            # 2026-11-08: maintainer capture() rejected the relative path).
            for state_key in ("nic_status", "nic_stdout_status", "mcu_status"):
                self.state.pop(state_key, None)
            self.state["mcu_restored"] = None
            self.state["mcu_safe_for_network"] = None
            self.counter.update(phase="reserved", child_pid=0, child_birth=None, child_stage="initialization")
            self.persist()  # Consume before any display/ROM/PG operation.
            self.publish("mcu", -errno.EINPROGRESS)
            self.display("mcu", -errno.EINPROGRESS)
            mcu_status = RUN / "boot" / ("mcu-attempt-%d.json" % self.counter["attempts"])
            args = [MCU, "--package", str(package), "--key", str(key), "--host-version", host,
                    "--status", str(mcu_status), "--sequence-floor", str(floor)]
            mcu_code, child = self.call("mcu", args, 180.0)
            mcu = read_json(mcu_status, 16384)
            self.state["mcu_restored"] = mcu.get("restored")
            self.state["mcu_safe_for_network"] = mcu.get("safe_for_network")
            self.state["mcu_status"] = mcu
            result = integer(mcu.get("status"), -4095, 0)
            valid_uid = mcu.get("uid")
            verified = (mcu_code == 0 and result == 0 and mcu.get("schema") == 1 and
                        mcu.get("completed") is True and mcu.get("verified") is True and
                        mcu.get("safe_for_network") is True and isinstance(valid_uid, str) and
                        re.fullmatch(r"[0-9a-f]{24}", valid_uid) and
                        valid_uid not in ("0" * 24, "f" * 24))
            if not child["normal_exit"] or child["overdue"]:
                self.ownership_known = False
                result = result or -errno.EOWNERDEAD
                break
            if mcu_attempt_needs_review(mcu):
                self.state["mcu_retry_blocked"] = True
                result = result or -errno.EIO
                self.counter.update(phase="settled", child_pid=0, child_birth=None,
                                    mcu_retry_blocked=True, status=result)
                self.persist()
                break
            if verified:
                self.publish("nic", -errno.EINPROGRESS)
                self.display("nic", -errno.EINPROGRESS)
                uid_code, uid_child = self.call("publish-uid", [HELPER, "--publish-uid", valid_uid], 2.0)
                if uid_code or uid_child["overdue"] or not uid_child["normal_exit"]:
                    raise BootError(uid_code or -errno.ETIMEDOUT, "uid", "UID publication incomplete")
                package_info = read_json(ASSETS / "package.json")
                if package_info.get("schema") != 1 or package_info.get("board", "Y4") != self.board:
                    raise BootError(-errno.EBADMSG, "nic", "board package metadata mismatch")
                if package_info.get("pg_module") == "omitted-read-only":
                    self.state["nic_mode"] = "inspect-only"
                    # finish collects the settled NIC snapshot and result.
                    result = 0
                else:
                    nic_code, nic_child = self.call("nic", [NIC, "--reconcile", "--attempt", str(self.counter["attempts"])], 300.0)
                    raw_summary = last_json(Path(nic_child["log"]), {"status", "restore_status", "verified_mask", "partial_unknown"})
                    receipt_path = RUN / "nic" / ("attempt-%d" % self.counter["attempts"]) / "receipt.json"
                    summary = read_json(receipt_path, 16384)
                    if summary.get("schema") != 1 or summary.get("completed") is not True:
                        self.ownership_known = False
                        self.state["nic_pending_receipt"] = summary
                        result = -errno.EOWNERDEAD
                        break
                    for field in ("read_mask", "mismatch_mask", "attempted_mask", "verified_mask", "after_valid_mask"):
                        integer(summary.get(field), 0, 7)
                    bind_failed = integer(summary.get("bind_failed_mask"), 0, 7)
                    if (type(summary.get("partial_unknown")) is not bool or
                        type(summary.get("cold_reload_required")) is not bool or
                        type(summary.get("network_restored")) is not bool):
                        raise ValueError("NIC receipt flags")
                    self.state["nic_status"] = summary
                    self.state["nic_stdout_status"] = raw_summary
                    result = integer(summary.get("status"), -4095, 0)
                    restore_status = integer(summary.get("restore_status"), -4095, 0)
                    self.state["nic_restore_status"] = restore_status
                    self.state["nic_bind_failed_mask"] = bind_failed
                    if restore_status and not bind_failed:
                        # No single port explains the restoration failure: this is
                        # a global PG/bus fault and blocks every port.
                        self.nic_restore_failed = True
                    elif restore_status == 0 and summary.get("network_restored") is not True:
                        self.nic_restore_failed = True
                    # A port that failed to rebind is isolated: the fresh
                    # inspection in finish() excludes it, and the remaining
                    # independently verified ports stay releasable.
                    if not nic_child["normal_exit"]:
                        self.ownership_known = False
                        break
                    complete = RUN / "nic" / ("attempt-%d" % self.counter["attempts"]) / "complete"
                    try:
                        marker = read_data(complete, 64).decode("ascii")
                        if not re.fullmatch(r"status=-?[0-9]+\n", marker):
                            raise ValueError("NIC completion marker")
                    except (OSError, ValueError, BootError):
                        if restore_status == 0:
                            self.ownership_known = False
                        break
                    if nic_code == 0 and result == 0 and summary.get("verified_mask") == 7 and not summary.get("partial_unknown") and not summary.get("cold_reload_required"):
                        result = 0
                    elif result == 0:
                        result = nic_code or -errno.EIO
            elif result == 0:
                result = mcu_code or -errno.EPROTO
            self.counter["history"].append({"attempt": self.counter["attempts"], "status": result,
                                            "mcu_status": mcu.get("status"), "nic_status": self.state.get("nic_status")})
            self.counter.update(phase="settled", child_pid=0, child_birth=None,
                                status=result, nic_restore_failed=self.nic_restore_failed)
            self.persist()
            if result == 0 or self.state.get("nic_mode") == "inspect-only":
                break
            # Immutable/authentication failures have no useful automatic repair.
            if result in (-errno.EPERM, -errno.EACCES, -errno.EKEYREJECTED, -errno.EBADMSG, -errno.EINVAL):
                break
            if self.nic_restore_failed:
                break
        if stopping:
            result = -errno.ECANCELED
        elif result and time.monotonic() >= self.deadline:
            self.state["last_operation_error"] = result
            result = -errno.ETIMEDOUT
        return self.finish(result, unknown=not self.ownership_known)

def mcu_attempt_needs_review(receipt):
    """Only retry a settled attempt with known MCU and pin state."""
    if (receipt.get("schema") != 1 or receipt.get("completed") is not True
            or receipt.get("restored") is not True
            or receipt.get("safe_for_network") is not True):
        return True
    if (receipt.get("recovery_probe") is True and receipt.get("erase_attempted") is True
            and receipt.get("verified") is not True):
        return True
    erased = receipt.get("erase_attempted")
    return type(erased) is not bool or (erased and receipt.get("readback_verified") is not True)


def last_json(path, required):
    data = read_data(path, 262144)
    for line in reversed(data.splitlines()):
        try:
            result = decode_json(line)
            if required.issubset(result):
                return result
        except (UnicodeError, ValueError):
            pass
    raise BootError(-errno.EBADMSG, "receipt", "missing final native receipt: " + str(path))

def ready():
    if os.geteuid():
        return 1
    try:
        current = boot_id()
        fd = trusted_dir(RUN, 0o700)
        os.close(fd)
        value = read_json(RUN / "initialization.json")
        counter = read_json(RUN / "boot-counter.json")
        if (counter.get("schema") != 1 or counter.get("boot_id") != current or
            counter.get("phase") != "complete" or counter.get("unknown_writer", False) is not False or
            counter.get("attempts") != value.get("attempt")):
            return 1
        if value.get("schema") != 1 or value.get("boot_id") != current or value.get("completed") is not True or value.get("network_safe") is not True or value.get("writers_settled") is not True:
            return 1
        integer(value.get("attempt"), 0, LIMIT)
        if value.get("pgdrv_loaded") is not False or value.get("pgdrv_status") != 0:
            return 1
        if value.get("max_attempts") != LIMIT or integer(value.get("usable_mask"), 0, 7) == 0:
            return 1
        if pending_barriers() or not lock_free(RUN / "boot.lock", False) or not lock_free(RUN / "nic.lock"):
            return 1
        return 0
    except (OSError, ValueError, UnicodeError, BootError):
        return 1

def is_y12_board(compatible=Path("/proc/device-tree/compatible")):
    try:
        return b"myt,y12" in compatible.read_bytes().split(b"\0")
    except FileNotFoundError:
        return False


def nic_inspection(result):
    if not isinstance(result, dict) or result.get("schema") != 1:
        raise ValueError("inspection schema")
    driver = integer(result.get("driver_mask"), 0, 7)
    usable = integer(result.get("usable_mask"), 0, 7)
    if usable & ~driver:
        raise ValueError("inspection mask")
    if type(result.get("pgdrv_loaded")) is not bool:
        raise ValueError("PG module observation")
    mac = result.get("bond_mac")
    if (usable & 6 != 6 or not isinstance(mac, str)
            or not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac)
            or int(mac[:2], 16) & 3 != 2):
        mac = None
    return {
        "observed_bond_mac": mac,
        "inspection_status": integer(result.get("status"), -4095, 0),
        "pgdrv_loaded": result["pgdrv_loaded"],
        "pgdrv_status": integer(result.get("pgdrv_status"), -4095, 0),
        "driver_mask": driver,
        "observed_usable_mask": usable,
    }


def main():
    y12 = is_y12_board()
    if len(sys.argv) == 2 and sys.argv[1] == "--ready":
        return ready()
    if len(sys.argv) != 1 or os.geteuid():
        print("Usage: y4-board-boot [--ready] (root required)", file=sys.stderr)
        return 2
    os.umask(0o077)
    for path in (RUN, STATE, RUN / "boot", RUN / "nic"):
        private_dir(path)
    lease = lock_file(RUN / "boot.lock", True)
    try:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Y4 initializer already owned", file=sys.stderr)
            return 1
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        obj = Initializer(boot_id(), "Y12" if y12 else "Y4")
        try:
            return obj.run()
        except (BootError, OSError, ValueError, UnicodeError) as error:
            code = error.code if isinstance(error, BootError) else -(getattr(error, "errno", None) or errno.EBADMSG)
            print("Y4 initialization error:", str(error), file=sys.stderr)
            obj.state["error_detail"] = str(error)
            if obj.child is not None:
                try:
                    obj.wait(obj.child, 0.0, False)
                except BaseException:
                    obj.ownership_known = False
            try:
                return obj.finish(code, unknown=not obj.ownership_known)
            except (BootError, OSError, ValueError):
                obj.publish("failed", code, completed=False, network_safe=False)
                return 0
    finally:
        os.close(lease)

if __name__ == "__main__":
    raise SystemExit(main())
