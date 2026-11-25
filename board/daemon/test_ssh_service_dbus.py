#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Exercise the SSH controller against an isolated D-Bus systemd peer.

Run with dbus-run-session -- python3 board/daemon/test_ssh_service_dbus.py
using Python's dbus and gi modules and the GIO development libraries.
"""
import os
import shlex
import subprocess
import tempfile
import threading
from pathlib import Path

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

SYSTEMD = "org.freedesktop.systemd1"
MANAGER = SYSTEMD + ".Manager"
PATH = "/org/freedesktop/systemd1"
SERVICE = "ssh.service"
SOCKET = "ssh.socket"


class MissingUnit(dbus.DBusException):
    _dbus_error_name = SYSTEMD + ".NoSuchUnit"


class AccessDenied(dbus.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.AccessDenied"


class Unit(dbus.service.Object):
    def __init__(self, bus, name, boot="disabled", active=False):
        self.name = name
        self.boot = boot
        self.active = active
        path = PATH + "/unit/" + name.replace(".", "_2e")
        super().__init__(bus, path)

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        assert interface == SYSTEMD + ".Unit"
        return {
            "LoadState": "masked" if self.boot == "masked" else "loaded",
            "ActiveState": self.active if isinstance(self.active, str) else
                           ("active" if self.active else "inactive"),
            "SubState": ("listening" if self.name == SOCKET else "running") if self.active else "dead",
        }


class Manager(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, PATH)
        self.units = {name: Unit(bus, name) for name in (SERVICE, SOCKET)}
        self.configure()
        self.nextid = 0

    def configure(self, s_boot="disabled", s_active=False, x_boot="disabled", x_active=False):
        self.units[SERVICE].boot = s_boot
        self.units[SERVICE].active = s_active
        self.units[SOCKET].boot = x_boot
        self.units[SOCKET].active = x_active
        self.fail_start = False
        self.delay_signal = False
        self.fail_disable = None
        self.operations = []
        self.subscriptions = set()

    def unit(self, name):
        unit = self.units[name]
        if unit.boot is None:
            raise MissingUnit("No such unit")
        return unit

    @dbus.service.method(MANAGER, in_signature="s", out_signature="s")
    def GetUnitFileState(self, name):
        return self.unit(name).boot

    @dbus.service.method(MANAGER, in_signature="s", out_signature="o")
    def LoadUnit(self, name):
        return self.unit(name).__dbus_object_path__

    @dbus.service.method(MANAGER, sender_keyword="sender")
    def Subscribe(self, sender=None):
        self.subscriptions.add(sender)

    @dbus.service.signal(MANAGER, signature="uoss")
    def JobRemoved(self, jobid, path, name, result):
        pass

    def job(self, name, mode, sender, start):
        assert sender in self.subscriptions
        assert mode == "fail"
        unit = self.unit(name)
        self.nextid += 1
        path = PATH + "/job/" + str(self.nextid)
        self.operations.append(("start" if start else "stop", name))
        if start and self.fail_start:
            result = "failed"
        else:
            unit.active = start
            result = "done"
        # Deliver a stale job completion and the target completion before
        # replying to StartUnit/StopUnit. The client must accept just its job.
        self.JobRemoved(0, PATH + "/job/0", name, "failed")
        if self.delay_signal:
            GLib.timeout_add(50, self.JobRemoved, self.nextid, path, name, result)
        else:
            self.JobRemoved(self.nextid, path, name, result)
        return path

    @dbus.service.method(MANAGER, in_signature="ss", out_signature="o", sender_keyword="sender")
    def StartUnit(self, name, mode, sender=None):
        return self.job(name, mode, sender, True)

    @dbus.service.method(MANAGER, in_signature="ss", out_signature="o", sender_keyword="sender")
    def StopUnit(self, name, mode, sender=None):
        return self.job(name, mode, sender, False)

    @dbus.service.method(MANAGER, in_signature="asbb", out_signature="ba(sss)")
    def EnableUnitFiles(self, names, runtime, force):
        assert not force
        self.operations.append(("enable", str(names[0]), bool(runtime)))
        self.unit(names[0]).boot = "enabled-runtime" if runtime else "enabled"
        return True, []

    @dbus.service.method(MANAGER, in_signature="asb", out_signature="a(sss)")
    def DisableUnitFiles(self, names, runtime):
        name = names[0]
        self.operations.append(("disable", str(name), bool(runtime)))
        unit = self.unit(name)
        if self.fail_disable == name:
            raise AccessDenied("Read-only policy")
        if unit.boot == ("enabled-runtime" if runtime else "enabled"):
            unit.boot = "disabled"
        return []

    @dbus.service.method(MANAGER)
    def Reload(self):
        self.operations.append(("reload",))


CLIENT = """
#include "ssh_service.h"
#include <errno.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv)
{
 if (argc != 2) return 2;
 struct y4_ssh_status state;
 int err = strcmp(argv[1], "status") == 0 ? y4_ssh_get_status(&state) :
           y4_ssh_set_enabled(strcmp(argv[1], "enable") == 0, &state);
 const char *result = err == 0 ? "ok" : err == -EPERM ? "masked" :
                      err == -EACCES ? "denied" : err == -EBUSY ? "busy" :
                      err == -EIO ? "failed" : "unexpected";
 printf("%s %d%d %d%d %d%d\\n", result, state.enabled, state.running,
        state.service_enabled, state.service_running, state.socket_enabled, state.socket_running);
 return 0;
}
"""


def run_client(path, option):
    env = {**os.environ, "DBUS_SYSTEM_BUS_ADDRESS": os.environ["DBUS_SESSION_BUS_ADDRESS"]}
    result = subprocess.run([path, option], text=True, check=True, capture_output=True,
                            env=env, timeout=20)
    return result.stdout.strip()


def tests(client, manager):
    manager.configure("enabled", True)
    assert run_client(client, "status") == "ok 11 11 00"
    assert not manager.operations
    assert run_client(client, "enable") == "ok 11 11 00"
    assert not manager.operations

    manager.configure(x_boot="enabled", x_active=True)
    assert run_client(client, "status") == "ok 11 00 11"
    assert run_client(client, "enable") == "ok 11 00 11"
    assert run_client(client, "disable") == "ok 00 00 00"
    assert manager.operations[0] == ("stop", SOCKET)
    assert run_client(client, "enable") == "ok 11 11 00"

    manager.configure("enabled", True, "enabled-runtime", True)
    assert run_client(client, "disable") == "ok 00 00 00"
    assert manager.operations[:2] == [("stop", SOCKET), ("stop", SERVICE)]
    assert ("disable", SOCKET, True) in manager.operations

    manager.configure("disabled", False, None)
    manager.delay_signal = True
    assert run_client(client, "enable") == "ok 11 11 00"
    assert run_client(client, "disable") == "ok 00 00 00"

    manager.configure("masked")
    assert run_client(client, "enable") == "masked 00 00 00"
    assert not manager.operations

    manager.configure("enabled", "activating")
    assert run_client(client, "disable") == "busy 10 10 00"
    assert not manager.operations

    manager.configure("masked", False, "enabled", True)
    assert run_client(client, "enable") == "masked 11 00 11"
    assert not manager.operations

    manager.configure()
    manager.fail_start = True
    assert run_client(client, "enable") == "failed 00 00 00"
    assert manager.operations == [("start", SERVICE)]

    manager.configure("enabled", True, "enabled", True)
    manager.fail_disable = SOCKET
    assert run_client(client, "disable") == "denied 11 11 11"
    assert manager.operations[-2:] == [("start", SOCKET), ("start", SERVICE)]


def main():
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    name = dbus.service.BusName(SYSTEMD, bus=bus, do_not_queue=True)
    manager = Manager(name)
    loop = GLib.MainLoop()
    errors = []

    def run():
        try:
            tests(client, manager)
        except Exception as error:
            errors.append(error)
        finally:
            GLib.idle_add(loop.quit)

    source = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="y4-ssh-service-test-") as temporary:
        main_file = Path(temporary) / "client.c"
        client = Path(temporary) / "client"
        main_file.write_text(CLIENT, encoding="utf-8")
        flags = shlex.split(subprocess.check_output(
            ["pkg-config", "--cflags", "--libs", "gio-2.0"], text=True))
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", source,
                        main_file, source / "ssh_service.c", "-o", client, *flags], check=True)
        runner = threading.Thread(target=run)
        runner.start()
        loop.run()
        runner.join()

    if errors:
        raise errors[0]
    print("SSH systemd D-Bus integration passed against the isolated mock")


if __name__ == "__main__":
    main()
