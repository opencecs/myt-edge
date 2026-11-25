/* SPDX-License-Identifier: Apache-2.0 */
#include "ssh_service.h"

#include <errno.h>
#include <gio/gio.h>
#include <string.h>

#define SYSTEMD "org.freedesktop.systemd1"
#define MANAGER SYSTEMD ".Manager"
#define SYSTEMD_PATH "/org/freedesktop/systemd1"

enum ssh_unit { SERVICE, SOCKET, SSH_UNIT_COUNT };
static const char *const unit_names[] = { "ssh.service", "ssh.socket" };
enum { BOOT_PERSISTENT = 1, BOOT_RUNTIME = 2 };
enum { CHANGE_FILES = 1, CHANGE_ACTIVE = 2 };

struct unit_state {
 bool present, running, active, transitioning, masked;
 unsigned int boot;
};

struct systemd_operation {
 GDBusConnection *bus;
 gint64 deadline;
 GMainContext *context;
 char *job_path;
 int job_result;
 bool job_pending;
};

static int bus_error(GError *error)
{
 if (!error) return -EIO;
 g_autofree gchar *remote = g_dbus_error_get_remote_error(error);
 if (g_strcmp0(remote, SYSTEMD ".UnitMasked") == 0) return -EPERM;
 if (g_strcmp0(remote, SYSTEMD ".NoSuchUnit") == 0 ||
     g_strcmp0(remote, SYSTEMD ".NoSuchUnitFile") == 0 ||
     g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_FILE_NOT_FOUND))
  return -ENOENT;
 if (g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED) ||
     g_error_matches(error, G_IO_ERROR, G_IO_ERROR_PERMISSION_DENIED))
  return -EACCES;
 if (g_error_matches(error, G_IO_ERROR, G_IO_ERROR_TIMED_OUT) ||
     g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_NO_REPLY) ||
     g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_TIMEOUT))
  return -ETIMEDOUT;
 if (g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_METHOD))
  return -EOPNOTSUPP;
 if (g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_SERVICE_UNKNOWN) ||
     g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_NAME_HAS_NO_OWNER) ||
     g_error_matches(error, G_IO_ERROR, G_IO_ERROR_CLOSED) ||
     g_error_matches(error, G_IO_ERROR, G_IO_ERROR_CONNECTION_REFUSED))
  return -ENOTCONN;
 return -EIO;
}

static int connect_systemd(struct systemd_operation *op)
{
 g_autoptr(GError) error = NULL;
 g_autofree gchar *address = g_dbus_address_get_for_bus_sync(G_BUS_TYPE_SYSTEM,
                                                            NULL, &error);
 if (!address) return -ENOTCONN;
 op->bus = g_dbus_connection_new_for_address_sync(address,
             G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT |
             G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,
             NULL, NULL, &error);
 return op->bus ? 0 : -ENOTCONN;
}

static int call(struct systemd_operation *op, const char *path, const char *interface,
                const char *method, GVariant *args, const GVariantType *reply_type,
                GVariant **reply)
{
 g_autoptr(GVariant) parameters = args ? g_variant_ref_sink(args) : NULL;
 g_autoptr(GError) error = NULL;
 gint64 left = op->deadline - g_get_monotonic_time();
 if (left <= 0) return -ETIMEDOUT;
 int timeout = (int) MIN((left + 999) / 1000, 4000);
 g_autoptr(GVariant) result = g_dbus_connection_call_sync(op->bus, SYSTEMD, path,
   interface, method, parameters, reply_type, G_DBUS_CALL_FLAGS_NO_AUTO_START,
   timeout, NULL, &error);
 if (!result) return bus_error(error);
 if (reply) *reply = g_steal_pointer(&result);
 return 0;
}

static int unit_state(struct systemd_operation *op, enum ssh_unit id,
                      struct unit_state *unit)
{
 g_autoptr(GVariant) files = NULL;
 g_autoptr(GVariant) loaded = NULL;
 g_autoptr(GVariant) props = NULL;
 g_autoptr(GVariant) fields = NULL;
 memset(unit, 0, sizeof *unit);

 int rc = call(op, SYSTEMD_PATH, MANAGER, "GetUnitFileState",
               g_variant_new("(s)", unit_names[id]), G_VARIANT_TYPE("(s)"), &files);
 if (rc && rc != -ENOENT) return rc;
 const char *file = NULL;
 if (files) g_variant_get(files, "(&s)", &file);
 if (g_strcmp0(file, "enabled") == 0) unit->boot = BOOT_PERSISTENT;
 if (g_strcmp0(file, "enabled-runtime") == 0) unit->boot = BOOT_RUNTIME;
 unit->masked = file && (strcmp(file, "masked") == 0 || strcmp(file, "masked-runtime") == 0);

 /* An already loaded unit can survive removal of its unit file. Read the
  * manager's live state so such an SSH listener can still be stopped. */
 rc = call(op, SYSTEMD_PATH, MANAGER, "LoadUnit", g_variant_new("(s)", unit_names[id]),
           G_VARIANT_TYPE("(o)"), &loaded);
 if (rc == -ENOENT && !file) return 0;
 if (rc) return rc;
 const char *path;
 g_variant_get(loaded, "(&o)", &path);
 rc = call(op, path, "org.freedesktop.DBus.Properties", "GetAll",
           g_variant_new("(s)", SYSTEMD ".Unit"), G_VARIANT_TYPE("(a{sv})"), &props);
 if (rc) return rc;
 g_variant_get(props, "(@a{sv})", &fields);
 const char *load_state, *active_state, *substate;
 if (!g_variant_lookup(fields, "LoadState", "&s", &load_state) ||
     !g_variant_lookup(fields, "ActiveState", "&s", &active_state) ||
     !g_variant_lookup(fields, "SubState", "&s", &substate))
  return -EIO;

 bool active = strcmp(active_state, "active") == 0 || strcmp(active_state, "reloading") == 0;
 unit->masked |= strcmp(load_state, "masked") == 0;
 unit->present = file || strcmp(load_state, "loaded") == 0 || active;
 unit->active = active;
 unit->running = active && (strcmp(substate, "running") == 0 ||
                 (id == SOCKET && strcmp(substate, "listening") == 0) ||
                 strcmp(active_state, "reloading") == 0);
 unit->transitioning = strcmp(active_state, "activating") == 0 ||
                        strcmp(active_state, "deactivating") == 0;
 return 0;
}

static int read_state(struct systemd_operation *op, struct unit_state units[SSH_UNIT_COUNT],
                      struct y4_ssh_status *status)
{
 for (enum ssh_unit i = SERVICE; i < SSH_UNIT_COUNT; ++i) {
  int rc = unit_state(op, i, &units[i]);
  if (rc) return rc;
 }
 if (!units[SERVICE].present && !units[SOCKET].present) return -ENOENT;
 status->service_enabled = (units[SERVICE].boot & BOOT_PERSISTENT) != 0;
 status->socket_enabled = (units[SOCKET].boot & BOOT_PERSISTENT) != 0;
 status->service_running = units[SERVICE].running;
 status->socket_running = units[SOCKET].running;
 status->enabled = status->service_enabled || status->socket_enabled;
 status->running = status->service_running || status->socket_running;
 return 0;
}

static int change_files(struct systemd_operation *op, enum ssh_unit id, bool enabled,
                        bool runtime)
{
 const char *names[] = { unit_names[id], NULL };
 if (enabled)
  return call(op, SYSTEMD_PATH, MANAGER, "EnableUnitFiles",
              g_variant_new("(^asbb)", names, runtime, FALSE),
              G_VARIANT_TYPE("(ba(sss))"), NULL);
 return call(op, SYSTEMD_PATH, MANAGER, "DisableUnitFiles",
             g_variant_new("(^asb)", names, runtime),
             G_VARIANT_TYPE("(a(sss))"), NULL);
}

static void job_removed(GDBusConnection *bus, const gchar *sender, const gchar *object_path,
                        const gchar *interface, const gchar *signal, GVariant *args,
                        gpointer user_data)
{
 (void)bus; (void)sender; (void)object_path; (void)interface; (void)signal;
 struct systemd_operation *op = user_data;
 const char *path, *result;
 if (!op->job_pending || !g_variant_is_of_type(args, G_VARIANT_TYPE("(uoss)"))) return;
 g_variant_get(args, "(u&o&s&s)", NULL, &path, NULL, &result);
 if (g_strcmp0(path, op->job_path) != 0) return;
 op->job_pending = false;
 if (strcmp(result, "done") == 0) op->job_result = 0;
 else if (strcmp(result, "timeout") == 0) op->job_result = -ETIMEDOUT;
 else if (strcmp(result, "canceled") == 0) op->job_result = -ECANCELED;
 else op->job_result = -EIO;
}

static gboolean job_deadline(gpointer user_data)
{
 struct systemd_operation *op = user_data;
 op->job_pending = false;
 op->job_result = -ETIMEDOUT;
 return G_SOURCE_REMOVE;
}

static int change_running(struct systemd_operation *op, enum ssh_unit id, bool running)
{
 g_autoptr(GVariant) reply = NULL;
 const char *method = running ? "StartUnit" : "StopUnit";
 int rc = call(op, SYSTEMD_PATH, MANAGER, method,
               g_variant_new("(ss)", unit_names[id], "fail"), G_VARIANT_TYPE("(o)"), &reply);
 if (rc) return rc;

 /* This private context is first dispatched here. Even if JobRemoved arrived
  * before the call replied, its callback sees the job path. */
 g_variant_get(reply, "(o)", &op->job_path);
 op->job_pending = true;
 g_autoptr(GSource) timer = g_timeout_source_new(0);
 g_source_set_ready_time(timer, op->deadline);
 g_source_set_callback(timer, job_deadline, op, NULL);
 g_source_attach(timer, op->context);
 while (op->job_pending && !g_dbus_connection_is_closed(op->bus))
  g_main_context_iteration(op->context, TRUE);
 g_source_destroy(timer);
 rc = op->job_pending ? -ENOTCONN : op->job_result;
 op->job_pending = false;
 if (rc == -ETIMEDOUT) {
  /* Cancel only the job returned for our own operation. The final read and
   * rollback still account for jobs that already started before cancellation. */
  gint64 saved = op->deadline;
  op->deadline = g_get_monotonic_time() + G_TIME_SPAN_SECOND;
  (void)call(op, op->job_path, SYSTEMD ".Job", "Cancel", NULL, G_VARIANT_TYPE_UNIT, NULL);
  op->deadline = saved;
 }
 g_clear_pointer(&op->job_path, g_free);
 return rc;
}

static int stop_ssh_units(struct systemd_operation *op,
                          const struct unit_state units[SSH_UNIT_COUNT],
                          unsigned int changes[SSH_UNIT_COUNT])
{
 for (int i = SOCKET; i >= SERVICE; --i) {
  if (!units[i].active && !units[i].transitioning) continue;
  changes[i] |= CHANGE_ACTIVE;
  int rc = change_running(op, (enum ssh_unit)i, false);
  if (rc) return rc;
 }
 return 0;
}

static int reload_unit_files(struct systemd_operation *op)
{
 return call(op, SYSTEMD_PATH, MANAGER, "Reload", NULL, G_VARIANT_TYPE_UNIT, NULL);
}

static int restore(struct systemd_operation *op, const struct unit_state before[SSH_UNIT_COUNT],
                   unsigned int changes[SSH_UNIT_COUNT], struct y4_ssh_status *status)
{
 struct unit_state now[SSH_UNIT_COUNT];
 int rc = read_state(op, now, status);
 if (rc) return rc;
 bool files_changed = false;
 int restore_error = 0;
 /* systemd 252 can lose track of a live sshd during Reload if the unit's
  * install alias sshd.service was added or removed. Quiesce both activation
  * paths before changing the alias back, then restore the previous mode. */
 if ((changes[SERVICE] & CHANGE_FILES) && before[SERVICE].boot != now[SERVICE].boot) {
  restore_error = stop_ssh_units(op, now, changes);
  rc = read_state(op, now, status);
  if (rc) return rc;
  if (now[SERVICE].active || now[SERVICE].transitioning ||
      now[SOCKET].active || now[SOCKET].transitioning)
   restore_error = -EBUSY;
 }
 for (enum ssh_unit i = SERVICE; !restore_error && i < SSH_UNIT_COUNT; ++i) {
  if (!(changes[i] & CHANGE_FILES) || before[i].boot == now[i].boot) continue;
  unsigned int remove = now[i].boot & ~before[i].boot;
  unsigned int add = before[i].boot & ~now[i].boot;
  rc = 0;
  if (remove & BOOT_PERSISTENT) rc = change_files(op, i, false, false);
  if (!rc && (remove & BOOT_RUNTIME)) rc = change_files(op, i, false, true);
  if (!rc && (add & BOOT_PERSISTENT)) rc = change_files(op, i, true, false);
  if (!rc && (add & BOOT_RUNTIME)) rc = change_files(op, i, true, true);
  if (rc) restore_error = rc;
  files_changed = true;
 }
 if (files_changed) {
  rc = reload_unit_files(op);
  if (rc) restore_error = rc;
 }
 for (int i = SOCKET; i >= SERVICE; --i) {
  if (!(changes[i] & CHANGE_ACTIVE)) continue;
  if (now[i].active == before[i].active && !now[i].transitioning) continue;
  rc = change_running(op, (enum ssh_unit)i, before[i].active);
  if (rc) restore_error = rc;
 }
 rc = read_state(op, now, status);
 if (rc) return rc;
 for (enum ssh_unit i = SERVICE; i < SSH_UNIT_COUNT; ++i) {
  if (((changes[i] & CHANGE_FILES) && before[i].boot != now[i].boot) ||
      ((changes[i] & CHANGE_ACTIVE) &&
       (before[i].active != now[i].active || now[i].transitioning)))
   return -EUCLEAN;
 }
 return restore_error;
}

static int set_state(struct systemd_operation *op, bool enabled, struct y4_ssh_status *status)
{
 struct unit_state before[SSH_UNIT_COUNT];
 struct unit_state after[SSH_UNIT_COUNT];
 unsigned int changes[SSH_UNIT_COUNT] = { 0 };
 int rc = read_state(op, before, status);
 if (rc) return rc;
 if (before[SERVICE].transitioning || before[SOCKET].transitioning) return -EBUSY;

 enum ssh_unit selected = SERVICE;
 if (enabled) {
  /* Preserve socket activation on installations already using it. Never
   * replace a healthy service with an idle socket or override a mask. */
  if (before[SOCKET].running || (!before[SERVICE].running && before[SOCKET].boot))
   selected = SOCKET;
  if (before[SERVICE].masked || before[selected].masked) return -EPERM;
  if (!before[SERVICE].present || !before[selected].present) return -ENOENT;
  if (!(before[selected].boot & BOOT_PERSISTENT)) {
   if (selected == SERVICE)
    rc = stop_ssh_units(op, before, changes);
   if (!rc) {
    changes[selected] |= CHANGE_FILES;
    rc = change_files(op, selected, true, false);
   }
   if (!rc) rc = reload_unit_files(op);
  }
  if (!rc && (!before[selected].running || (changes[selected] & CHANGE_ACTIVE))) {
   changes[selected] |= CHANGE_ACTIVE;
   rc = change_running(op, selected, true);
  }
 } else {
  /* First close socket activation so it cannot restart ssh.service while the
   * service stop job runs. Existing login processes follow the unit's KillMode. */
  rc = stop_ssh_units(op, before, changes);
  for (enum ssh_unit i = SERVICE; !rc && i < SSH_UNIT_COUNT; ++i) {
   if (!before[i].boot) continue;
   changes[i] |= CHANGE_FILES;
   rc = change_files(op, i, false, false);
   if (!rc) rc = change_files(op, i, false, true);
  }
  if (!rc && ((changes[SERVICE] | changes[SOCKET]) & CHANGE_FILES))
   rc = reload_unit_files(op);
 }
 if (!rc) rc = read_state(op, after, status);
 if (!rc && enabled &&
     (!after[selected].running || !(after[selected].boot & BOOT_PERSISTENT)))
  rc = -EIO;
 if (!rc && !enabled &&
     (after[SERVICE].active || after[SOCKET].active || after[SERVICE].boot ||
      after[SOCKET].boot || after[SERVICE].transitioning || after[SOCKET].transitioning))
  rc = -EIO;
 if (rc && (changes[SERVICE] || changes[SOCKET])) {
  op->deadline = g_get_monotonic_time() + 5 * G_TIME_SPAN_SECOND;
  if (restore(op, before, changes, status)) return -EUCLEAN;
 }
 return rc;
}

int y4_ssh_get_status(struct y4_ssh_status *status)
{
 struct systemd_operation op = { 0 };
 struct unit_state units[SSH_UNIT_COUNT];
 memset(status, 0, sizeof *status);
 int rc = connect_systemd(&op);
 if (rc) return rc;
 op.deadline = g_get_monotonic_time() + 3 * G_TIME_SPAN_SECOND;
 rc = read_state(&op, units, status);
 g_object_unref(op.bus);
 return rc;
}

int y4_ssh_set_enabled(bool enabled, struct y4_ssh_status *status)
{
 struct systemd_operation op = { 0 };
 memset(status, 0, sizeof *status);
 int rc = connect_systemd(&op);
 if (rc) return rc;
 op.context = g_main_context_new();
 g_main_context_push_thread_default(op.context);
 guint subscription = g_dbus_connection_signal_subscribe(op.bus, SYSTEMD, MANAGER,
                     "JobRemoved", SYSTEMD_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE,
                     job_removed, &op, NULL);
 op.deadline = g_get_monotonic_time() + 10 * G_TIME_SPAN_SECOND;
 rc = call(&op, SYSTEMD_PATH, MANAGER, "Subscribe", NULL, G_VARIANT_TYPE_UNIT, NULL);
 if (!rc) rc = set_state(&op, enabled, status);
 g_dbus_connection_signal_unsubscribe(op.bus, subscription);
 g_main_context_pop_thread_default(op.context);
 g_main_context_unref(op.context);
 g_object_unref(op.bus);
 return rc;
}
