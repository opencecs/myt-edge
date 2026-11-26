#!/usr/bin/env python3
"""Exercise the actual OTA reboot request with real GLib variants and IPC stubs.

Requires a host C compiler and pkg-config gio-2.0, already used by y4-boardd.
No system/session D-Bus connection is opened and no reboot is requested.
Only y4_board_request_reboot() is extracted; transport calls are intercepted, while
GVariant encoding/type checks, GError allocation and GObject cleanup use GLib.
--source allows a negative control against an earlier or mutated ota.c.

systemd v252 references:
https://github.com/systemd/systemd/blob/v252/src/core/dbus-manager.c
https://github.com/systemd/systemd/blob/v252/src/systemctl/systemctl-start-unit.c
"""
import argparse
import os
import pathlib
import resource
import shlex
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

HARNESS = r"""
#include <gio/gio.h>
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

enum scenario {
  SUCCESS, BUS_ERROR, BUS_NO_ERROR, ACCESS_DENIED, METHOD_ERROR,
  CALL_TIMEOUT, CALL_NO_ERROR, WRONG_REPLY_TYPE
};
static enum scenario scenario;
static unsigned int bus_calls, method_calls, bus_unrefs, bus_finalized;
static unsigned int reply_unrefs, error_frees;
static GObject *bus_object;
static GVariant *reply_object;

static void finalized(gpointer unused, GObject *object) {
  (void)unused;
  assert(object == bus_object);
  bus_finalized++;
}
static GDBusConnection *fake_bus_get_sync(GBusType kind,
                                         GCancellable *cancellable,
                                         GError **error) {
  bus_calls++;
  assert(kind == G_BUS_TYPE_SYSTEM && cancellable == NULL);
  assert(error != NULL && *error == NULL);
  if (scenario == BUS_ERROR || scenario == BUS_NO_ERROR) {
    if (scenario == BUS_ERROR)
      g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_NOT_CONNECTED,
                          "injected bus failure");
    return NULL;
  }
  bus_object = g_object_new(G_TYPE_OBJECT, NULL);
  g_object_weak_ref(bus_object, finalized, NULL);
  return (GDBusConnection *)bus_object;
}
static GVariant *fake_call_sync(GDBusConnection *connection,
                                const gchar *destination,
                                const gchar *object_path,
                                const gchar *interface_name,
                                const gchar *method_name,
                                GVariant *parameters,
                                const GVariantType *reply_type,
                                GDBusCallFlags flags, gint timeout,
                                GCancellable *cancellable, GError **error) {
  const gchar *unit, *mode;
  method_calls++;
  assert(connection == (GDBusConnection *)bus_object);
  assert(strcmp(destination, "org.freedesktop.systemd1") == 0);
  assert(strcmp(object_path, "/org/freedesktop/systemd1") == 0);
  assert(strcmp(interface_name, "org.freedesktop.systemd1.Manager") == 0);
  assert(strcmp(method_name, "StartUnit") == 0);
  assert(parameters != NULL);
  assert(g_variant_is_of_type(parameters, G_VARIANT_TYPE("(ss)")));
  assert(g_variant_is_floating(parameters));
  g_variant_ref_sink(parameters);
  g_variant_get(parameters, "(&s&s)", &unit, &mode);
  assert(strcmp(unit, "reboot.target") == 0);
  assert(strcmp(mode, "replace-irreversibly") == 0);
  assert(reply_type != NULL);
  assert(g_variant_type_equal(reply_type, G_VARIANT_TYPE("(o)")));
  assert(flags == G_DBUS_CALL_FLAGS_NO_AUTO_START && timeout == 30000);
  assert(cancellable == NULL && error != NULL && *error == NULL);
  /* GIO consumes floating parameters even when the request fails. */
  g_variant_unref(parameters);

  if (scenario == ACCESS_DENIED || scenario == METHOD_ERROR) {
    g_set_error_literal(error, G_DBUS_ERROR,
                        scenario == ACCESS_DENIED ? G_DBUS_ERROR_ACCESS_DENIED :
                                                    G_DBUS_ERROR_UNKNOWN_METHOD,
                        "injected remote method failure");
    return NULL;
  }
  if (scenario == CALL_TIMEOUT) {
    g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_TIMED_OUT,
                        "injected transport timeout");
    return NULL;
  }
  if (scenario == CALL_NO_ERROR)
    return NULL;

  GVariant *reply = g_variant_ref_sink(
      scenario == WRONG_REPLY_TYPE ? g_variant_new("(s)", "not a job") :
      g_variant_new("(o)", "/org/freedesktop/systemd1/job/42"));
  /* Model GIO rejecting a response which violates the requested type. */
  if (!g_variant_is_of_type(reply, reply_type)) {
    g_variant_unref(reply);
    g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
                        "injected wrong reply type");
    return NULL;
  }
  reply_object = reply;
  return reply;
}
static void counted_object_unref(gpointer object) {
  assert(object == bus_object);
  bus_unrefs++;
  g_object_unref(object);
}
static void counted_variant_unref(GVariant *value) {
  assert(value == reply_object);
  reply_unrefs++;
  g_variant_unref(value);
}
static void counted_error_free(GError *error) {
  assert(error != NULL);
  error_frees++;
  g_error_free(error);
}

#define g_bus_get_sync fake_bus_get_sync
#define g_dbus_connection_call_sync fake_call_sync
#undef g_object_unref
#define g_object_unref counted_object_unref
#define g_variant_unref counted_variant_unref
#define g_error_free counted_error_free
"""

TESTS = r"""
#undef g_bus_get_sync
#undef g_dbus_connection_call_sync
#undef g_object_unref
#undef g_variant_unref
#undef g_error_free

static void run_case(enum scenario test) {
  scenario = test;
  bus_calls = method_calls = bus_unrefs = bus_finalized = 0;
  reply_unrefs = error_frees = 0;
  bus_object = NULL;
  reply_object = NULL;

  int result = y4_board_request_reboot();
  assert(bus_calls == 1);
  if (test == BUS_ERROR || test == BUS_NO_ERROR) {
    assert(result == -ENOSYS);
    assert(method_calls == 0 && bus_unrefs == 0 && bus_finalized == 0);
  } else {
    assert(result == (test == SUCCESS ? 0 : -EIO));
    assert(method_calls == 1 && bus_unrefs == 1 && bus_finalized == 1);
  }
  assert(reply_unrefs == (test == SUCCESS ? 1U : 0U));
  assert(error_frees == ((test == BUS_ERROR || test == ACCESS_DENIED ||
                         test == METHOD_ERROR || test == CALL_TIMEOUT ||
                         test == WRONG_REPLY_TYPE) ? 1U : 0U));
}
int main(void) {
  for (int test = SUCCESS; test <= WRONG_REPLY_TYPE; test++)
    run_case(test);
  puts("Actual y4_board_request_reboot: 8 cases passed (message, variants, errors, cleanup)");
  return 0;
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=pathlib.Path,
                        default=ROOT / "board/daemon/ota.c")
    args = parser.parse_args()
    source = args.source.read_text()
    symbol = "int y4_board_request_reboot(void)"
    legacy = "static int request_reboot(void)"
    if symbol not in source:
        source = source.replace(legacy, symbol, 1)
    start = source.index(symbol)
    function = source[start:source.index("static void ota_release(void)", start)]
    flags = shlex.split(subprocess.check_output(
        ["pkg-config", "--cflags", "--libs", "gio-2.0"], text=True))
    compiler = shlex.split(os.environ.get("CC", "cc"))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with tempfile.TemporaryDirectory(prefix="ota-reboot-test-") as temp:
        cfile = pathlib.Path(temp) / "test.c"
        binary = pathlib.Path(temp) / "test"
        cfile.write_text(HARNESS + function + TESTS)
        subprocess.run(compiler + ["-std=c11", "-Wall", "-Wextra", "-Werror",
                                   str(cfile), "-o", str(binary)] + flags,
                       check=True)
        return subprocess.run([str(binary)]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
