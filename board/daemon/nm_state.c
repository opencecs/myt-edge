/* SPDX-License-Identifier: Apache-2.0 */
/* Read-only observation of the standard Debian network manager state for the
 * two logical Y4 interfaces. The kernel cannot distinguish an ongoing DHCP
 * exchange from a failed or absent one, so the DHCP phase comes from the
 * network manager itself. Every call only reads properties: NO_AUTO_START,
 * a bounded timeout, no Reapply, no CheckConnectivity, no activation. */
#define _GNU_SOURCE
#include "board_daemon.h"
#include <gio/gio.h>
#include <errno.h>
#define NM_NAME "org.freedesktop.NetworkManager"
#define NM_PATH "/org/freedesktop/NetworkManager"
#define NM_DEVICE_IFACE NM_NAME ".Device"
#define NM_QUERY_TIMEOUT_MS 300
/* Single-caller module: the monitor loop owns the cached connection. */
int y4_board_nm_states(struct y4_network_manager_state out[Y4_NET_LOGICAL_COUNT]) {
 static GDBusConnection *bus;
 static int bus_failed;
 if(!out)return -EINVAL;
 for(unsigned i=0;i<Y4_NET_LOGICAL_COUNT;i++){out[i].valid=0;out[i].state=0;}
 if(bus_failed)return -ENOSYS;
 if(!bus){
  GError *error=NULL;
  bus=g_bus_get_sync(G_BUS_TYPE_SYSTEM,NULL,&error);
  if(!bus){bus_failed=1;if(error)g_error_free(error);return -ENOSYS;}
 }
 const char *const ifaces[Y4_NET_LOGICAL_COUNT]={
  !strcmp(Y4_DEVICE_ID,"Y12")?"y12lan":"y4lan",Y4_NET_BOND_NAME};
 for(unsigned i=0;i<Y4_NET_LOGICAL_COUNT;i++){
  GError *error=NULL;
  GVariant *reply=g_dbus_connection_call_sync(bus,NM_NAME,NM_PATH,NM_NAME,"GetDeviceByIpIface",
   g_variant_new("(s)",ifaces[i]),G_VARIANT_TYPE("(o)"),G_DBUS_CALL_FLAGS_NO_AUTO_START,
   NM_QUERY_TIMEOUT_MS,NULL,&error);
  if(!reply){g_error_free(error);continue;} /* Absent device: state stays unknown. */
  const char *path=NULL;g_variant_get(reply,"(&o)",&path);
  GVariant *props=g_dbus_connection_call_sync(bus,NM_NAME,path,"org.freedesktop.DBus.Properties",
   "GetAll",g_variant_new("(s)",NM_DEVICE_IFACE),G_VARIANT_TYPE("(a{sv})"),
   G_DBUS_CALL_FLAGS_NO_AUTO_START,NM_QUERY_TIMEOUT_MS,NULL,&error);
  g_variant_unref(reply);
  if(!props){g_error_free(error);continue;}
  GVariant *dictionary=NULL; /* GetAll replies with (a{sv}): unwrap it. */
  g_variant_get(props,"(@a{sv})",&dictionary);
  g_variant_unref(props);
  if(!dictionary)continue;
  props=dictionary;
  guint32 state=0;
  if(g_variant_lookup(props,"State","u",&state)){out[i].valid=1;out[i].state=(int)state;}
  g_variant_unref(props);
 }
 return 0;
}
