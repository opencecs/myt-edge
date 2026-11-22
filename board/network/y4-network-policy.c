/* SPDX-License-Identifier: Apache-2.0 */
/*
 * NetworkManager remains the only DHCP/routing/DNS owner. This small adapter
 * uses Device.GetAppliedConnection/Reapply, with version CAS, on two fixed
 * product profiles. Kernel state is queried through y4_network_sample().
 * It never sends RTM_NEW*, execs network tools, edits resolv.conf, bridges,
 * forwards, or runs a second DHCP client.
 */
#define _GNU_SOURCE
#include "y4_network.h"
#include <gio/gio.h>
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/fib_rules.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define NM_SERVICE "org.freedesktop.NetworkManager"
#define NM_ROOT "/org/freedesktop/NetworkManager"
#define NM_DEVICE NM_SERVICE ".Device"
#define NM_ACTIVE NM_SERVICE ".Connection.Active"
#define NM_SETTINGS NM_SERVICE ".Settings.Connection"
#define MAX_ADDRESSES 32u
#define MAX_ROUTE_ENTRIES 128u
#define MAX_PROJECTED_ROUTE_ENTRIES (MAX_ROUTE_ENTRIES/2u + 2u*MAX_ADDRESSES + 1u)
#define DBUS_TIMEOUT_MS 1500
struct address { unsigned char bytes[16]; char text[INET6_ADDRSTRLEN]; unsigned prefix; };
struct learned_route { struct address dest; char gateway[INET6_ADDRSTRLEN]; unsigned metric; };
struct ip_state { int family; struct address addresses[MAX_ADDRESSES]; unsigned count; char gateway[INET6_ADDRSTRLEN];
                  struct learned_route learned[MAX_ADDRESSES]; unsigned learned_count; };
struct device {
    int active,status;
    unsigned table,metric;
    char path[256],profile[256];
    guint64 version;
    GVariant *applied,*baseline;
    struct ip_state ip[2];
};
#ifndef Y4_NET_LAN_NAME
#define Y4_NET_LAN_NAME "y4lan"
#endif
static const char *const names[]={Y4_NET_LAN_NAME,Y4_NET_BOND_NAME};
static const char *const uuids[]={"948fe2c1-4018-4acf-b25b-ecedb97c2500","948fe2c1-4018-4acf-b25b-ecedb97c1000"};
static volatile sig_atomic_t stopping;
static void stop(int sig){(void)sig;stopping=1;}
static uint64_t milliseconds(void)
{
    struct timespec t;
    if(clock_gettime(CLOCK_MONOTONIC,&t)||t.tv_sec<0)return 0;
    return (uint64_t)t.tv_sec*1000u+(uint64_t)t.tv_nsec/1000000u;
}
static GVariant *call(GDBusConnection *bus,const char *path,const char *iface,const char *method,
                       GVariant *args,const GVariantType *reply_type,int *status)
{
    GError *error=NULL;
    GVariant *reply=g_dbus_connection_call_sync(bus,NM_SERVICE,path,iface,method,args,reply_type,
                           G_DBUS_CALL_FLAGS_NO_AUTO_START,DBUS_TIMEOUT_MS,NULL,&error);
    if(!reply){
        *status=error&&g_error_matches(error,G_IO_ERROR,G_IO_ERROR_TIMED_OUT)?-ETIMEDOUT:-EHOSTUNREACH;
        if(error){
            char *remote=g_dbus_error_get_remote_error(error);
            if(remote&&(g_str_has_suffix(remote,".UnknownDevice")||g_str_has_suffix(remote,".UnknownObject")))*status=-ENODEV;
            else if(remote&&g_str_has_suffix(remote,".VersionIdMismatch"))*status=-ESTALE;
            g_free(remote);g_error_free(error);
        }
    }
    return reply;
}
static GVariant *properties(GDBusConnection *bus,const char *path,const char *iface,int *status)
{
    GVariant *reply=call(bus,path,"org.freedesktop.DBus.Properties","GetAll",g_variant_new("(s)",iface),G_VARIANT_TYPE("(a{sv})"),status);
    if(!reply)return NULL;
    GVariant *result=NULL;g_variant_get(reply,"(@a{sv})",&result);g_variant_unref(reply);return result;
}
static int copy_path(char out[256],const char *value)
{
    if(!value||value[0]!='/'||strlen(value)>=256)return -EPROTO;
    memcpy(out,value,strlen(value)+1u);return 0;
}
static int ip_bytes(int family,const char *text,unsigned char bytes[16],int allow_link_local)
{
    if(!text||!inet_pton(family,text,bytes))return 0;
    if(family==AF_INET){
        uint32_t host;memcpy(&host,bytes,4);host=ntohl(host);
        if(!host||(host>>24)==127u||(host>>28)>=14u)return 0;
        if(!allow_link_local&&(host>>16)==0xa9feu)return 0;
    }else{
        static const unsigned char zero[16]={0},loopback[16]={0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1};
        if(!memcmp(bytes,zero,16)||!memcmp(bytes,loopback,16)||bytes[0]==0xff)return 0;
        if(!allow_link_local&&bytes[0]==0xfe&&(bytes[1]&0xc0u)==0x80u)return 0;
    }
    return 1;
}
static int load_ip(GDBusConnection *bus,const char *path,int family,unsigned table,struct ip_state *out)
{
    memset(out,0,sizeof *out);out->family=family;
    if(!path||!strcmp(path,"/"))return 0;
    int rc=0;
    GVariant *props=properties(bus,path,family==AF_INET?NM_SERVICE ".IP4Config":NM_SERVICE ".IP6Config",&rc);
    if(!props)return rc;
    GVariant *addresses=g_variant_lookup_value(props,"AddressData",G_VARIANT_TYPE("aa{sv}"));
    if(!addresses){g_variant_unref(props);return -EPROTO;}
    if(g_variant_n_children(addresses)>MAX_ADDRESSES){g_variant_unref(addresses);g_variant_unref(props);return -E2BIG;}
    GVariantIter iter;g_variant_iter_init(&iter,addresses);GVariant *entry;
    while((entry=g_variant_iter_next_value(&iter))){
        const char *text=NULL;guint32 prefix=0;struct address next={0};
        if(!g_variant_lookup(entry,"address","&s",&text)||!g_variant_lookup(entry,"prefix","u",&prefix)||prefix>(family==AF_INET?32u:128u)){
            rc=-EPROTO;g_variant_unref(entry);break;
        }
        if(ip_bytes(family,text,next.bytes,0)){
            next.prefix=prefix;
            if(!inet_ntop(family,next.bytes,next.text,sizeof next.text)){rc=-errno;g_variant_unref(entry);break;}
            out->addresses[out->count++]=next;
        }
        g_variant_unref(entry);
    }
    g_variant_unref(addresses);
    const char *gateway=NULL;unsigned char raw[16]={0};
    if(!rc&&g_variant_lookup(props,"Gateway","&s",&gateway)&&ip_bytes(family,gateway,raw,1))
        (void)inet_ntop(family,raw,out->gateway,sizeof out->gateway);
    /* DHCP classless routes and the on-device default route both appear in
     * RouteData. Their lifecycle stays with NetworkManager: every cycle the
     * main-table projection is rebuilt from the live IP4Config, so a route
     * dropped by DHCP disappears here as well. */
    if(!rc){
        GVariant *routes=g_variant_lookup_value(props,"RouteData",G_VARIANT_TYPE("aa{sv}"));
        if(routes){
            if(g_variant_n_children(routes)>MAX_ROUTE_ENTRIES)rc=-E2BIG;
            g_variant_iter_init(&iter,routes);
            while(!rc&&(entry=g_variant_iter_next_value(&iter))){
                guint32 prefix=1,route_table=table,metric=0;
                const char *hop=NULL,*dest=NULL;
                (void)g_variant_lookup(entry,"table","u",&route_table);
                if(g_variant_lookup(entry,"prefix","u",&prefix)&&route_table==table&&
                   g_variant_lookup(entry,"dest","&s",&dest)){
                    struct learned_route next={0};
                    if(!prefix&&g_variant_lookup(entry,"next-hop","&s",&hop)&&ip_bytes(family,hop,raw,1)){
                        (void)inet_ntop(family,raw,out->gateway,sizeof out->gateway);
                    }else if(prefix&&(prefix<=(family==AF_INET?32u:128u))&&ip_bytes(family,dest,next.dest.bytes,0)){
                        /* Non-default learned route; next-hop is optional for
                         * on-link DHCP routes and the metric is kept as NM
                         * reported it so conflicts resolve by kernel metric. */
                        (void)g_variant_lookup(entry,"metric","u",&metric);
                        if(metric<=0xFFFFu){
                            int have_hop=g_variant_lookup(entry,"next-hop","&s",&hop)&&ip_bytes(family,hop,raw,1)&&
                                         inet_ntop(family,raw,next.gateway,sizeof next.gateway);
                            if(!have_hop)next.gateway[0]=0;
                            next.dest.prefix=prefix;
                            next.metric=metric;
                            if(inet_ntop(family,next.dest.bytes,next.dest.text,sizeof next.dest.text)){
                                if(out->learned_count>=G_N_ELEMENTS(out->learned))rc=-E2BIG;
                                else out->learned[out->learned_count++]=next;
                            }
                        }
                    }
                }
                g_variant_unref(entry);
            }
            g_variant_unref(routes);
        }
    }
    g_variant_unref(props);return rc;
}
static void device_clear(struct device *d)
{
    if(d->applied)g_variant_unref(d->applied);
    if(d->baseline)g_variant_unref(d->baseline);
    memset(d,0,sizeof *d);
}
static int load_device(GDBusConnection *bus,unsigned role,struct device *d)
{
    memset(d,0,sizeof *d);d->table=role?1010u:1025u;d->metric=role?100u:600u;
    int rc=0;GVariant *reply=call(bus,NM_ROOT,NM_SERVICE,"GetDeviceByIpIface",g_variant_new("(s)",names[role]),G_VARIANT_TYPE("(o)"),&rc);
    if(!reply)return rc;
    const char *path=NULL;g_variant_get(reply,"(&o)",&path);rc=copy_path(d->path,path);g_variant_unref(reply);if(rc)return rc;
    GVariant *props=properties(bus,d->path,NM_DEVICE,&rc);if(!props)return rc;
    guint32 state=0;const char *active=NULL,*ip4=NULL,*ip6=NULL;
    if(!g_variant_lookup(props,"State","u",&state)||!g_variant_lookup(props,"ActiveConnection","&o",&active)||
       !g_variant_lookup(props,"Ip4Config","&o",&ip4)||!g_variant_lookup(props,"Ip6Config","&o",&ip6)){g_variant_unref(props);return -EPROTO;}
    if(state!=100u||!strcmp(active,"/")){g_variant_unref(props);return 0;}
    GVariant *a=properties(bus,active,NM_ACTIVE,&rc);if(!a){g_variant_unref(props);return rc;}
    const char *uuid=NULL,*profile=NULL;
    if(!g_variant_lookup(a,"Uuid","&s",&uuid)||strcmp(uuid,uuids[role])||
       !g_variant_lookup(a,"Connection","&o",&profile)){g_variant_unref(a);g_variant_unref(props);return -EPERM;}
    rc=copy_path(d->profile,profile);g_variant_unref(a);if(rc){g_variant_unref(props);return rc;}
    reply=call(bus,d->path,NM_DEVICE,"GetAppliedConnection",g_variant_new("(u)",0u),G_VARIANT_TYPE("(a{sa{sv}}t)"),&rc);
    if(!reply){g_variant_unref(props);return rc;}
    g_variant_get(reply,"(@a{sa{sv}}t)",&d->applied,&d->version);g_variant_unref(reply);
    reply=call(bus,d->profile,NM_SETTINGS,"GetSettings",NULL,G_VARIANT_TYPE("(a{sa{sv}})"),&rc);
    if(!reply){g_variant_unref(props);return rc;}
    g_variant_get(reply,"(@a{sa{sv}})",&d->baseline);g_variant_unref(reply);
    for(unsigned family=0;family<2u;family++){
        const char *setting=family?"ipv6":"ipv4";
        GVariant *base_ip=g_variant_lookup_value(d->baseline,setting,G_VARIANT_TYPE("a{sv}"));
        guint32 route_table=0;
        if(!base_ip||!g_variant_lookup(base_ip,"route-table","u",&route_table)||route_table!=d->table)rc=-EOPNOTSUPP;
        if(base_ip)g_variant_unref(base_ip);
        if(rc)break;
    }
    if(!rc)rc=load_ip(bus,ip4,AF_INET,d->table,&d->ip[0]);
    if(!rc)rc=load_ip(bus,ip6,AF_INET6,d->table,&d->ip[1]);
    g_variant_unref(props);
    if(!rc)d->active=1;
    return rc;
}
static int is_default_rule_action(const char *key,GVariant *value)
{
    return !strcmp(key,"action")&&g_variant_is_of_type(value,G_VARIANT_TYPE_BYTE)&&
           g_variant_get_byte(value)==FR_ACT_TO_TBL;
}
/* Dictionary field order and route/rule ordering are immaterial to NM. */
static int equivalent(GVariant *a,GVariant *b)
{
    if(!a||!b)return a==b;
    if(!g_variant_is_of_type(a,g_variant_get_type(b)))return 0;
    if(g_variant_is_of_type(a,G_VARIANT_TYPE("a{sv}"))){
        /* NM normalizes routing rules: the default action (to-table) may be
         * present in the profile and gone from the applied connection, or the
         * other way around after an NM upgrade. An explicit action byte with
         * the default value therefore denotes the same rule as an absent
         * key on either side; every other key must match exactly. */
        gsize effective[2]={0,0};
        GVariant *sides[2]={a,b};
        for(unsigned side=0;side<2u;side++){
            GVariantIter iter;const char *key;GVariant *value;
            g_variant_iter_init(&iter,sides[side]);
            while(g_variant_iter_next(&iter,"{&sv}",&key,&value)){
                if(!is_default_rule_action(key,value))effective[side]++;
                g_variant_unref(value);
            }
        }
        if(effective[0]!=effective[1])return 0;
        GVariantIter iter;const char *key;GVariant *value;g_variant_iter_init(&iter,a);
        while(g_variant_iter_next(&iter,"{&sv}",&key,&value)){
            int default_action=is_default_rule_action(key,value);
            int same=1;
            if(!default_action){
                GVariant *other=g_variant_lookup_value(b,key,NULL);
                same=other&&equivalent(value,other);
                if(other)g_variant_unref(other);
            }
            g_variant_unref(value);if(!same)return 0;
        }
        return 1;
    }
    if(g_variant_is_of_type(a,G_VARIANT_TYPE("aa{sv}"))){
        gsize n=g_variant_n_children(a);if(n!=g_variant_n_children(b)||n>MAX_PROJECTED_ROUTE_ENTRIES)return 0;
        unsigned char used[MAX_PROJECTED_ROUTE_ENTRIES]={0};
        for(gsize i=0;i<n;i++){
            GVariant *item=g_variant_get_child_value(a,i);int found=0;
            for(gsize j=0;j<n;j++)if(!used[j]){
                GVariant *other=g_variant_get_child_value(b,j);int same=equivalent(item,other);g_variant_unref(other);
                if(same){used[j]=1;found=1;break;}
            }
            g_variant_unref(item);if(!found)return 0;
        }
        return 1;
    }
    return g_variant_equal(a,b);
}
static int array_copy(GVariantBuilder *out,GVariant *dict,const char *key)
{
    GVariant *array=g_variant_lookup_value(dict,key,G_VARIANT_TYPE("aa{sv}"));
    if(!array)return 0;
    if(g_variant_n_children(array)>MAX_ROUTE_ENTRIES/2u){g_variant_unref(array);return -E2BIG;}
    GVariantIter iter;g_variant_iter_init(&iter,array);GVariant *entry;
    while((entry=g_variant_iter_next_value(&iter))){g_variant_builder_add_value(out,entry);g_variant_unref(entry);}
    g_variant_unref(array);return 0;
}
static void route(GVariantBuilder *routes,const struct address *address,const char *gateway,
                  unsigned table,unsigned metric,int is_default,int family,const char *source)
{
    unsigned char bytes[16]={0};size_t size=family==AF_INET?4u:16u;
    memcpy(bytes,address->bytes,size);
    if(is_default)memset(bytes,0,size);
    else for(size_t i=0;i<size;i++){
        unsigned bit=(unsigned)i*8u;
        bytes[i]=bit>=address->prefix?0:(unsigned char)(bytes[i]&(address->prefix-bit>=8u?255u:(255u<<(8u-address->prefix+bit))));
    }
    char dest[INET6_ADDRSTRLEN];(void)inet_ntop(family,bytes,dest,sizeof dest);
    GVariantBuilder entry;g_variant_builder_init(&entry,G_VARIANT_TYPE("a{sv}"));
    g_variant_builder_add(&entry,"{sv}","dest",g_variant_new_string(dest));
    g_variant_builder_add(&entry,"{sv}","prefix",g_variant_new_uint32(is_default?0u:address->prefix));
    g_variant_builder_add(&entry,"{sv}","table",g_variant_new_uint32(table));
    g_variant_builder_add(&entry,"{sv}","metric",g_variant_new_uint32(metric));
    g_variant_builder_add(&entry,"{sv}","src",g_variant_new_string(source&&source[0]?source:address->text));
    if(gateway&&gateway[0]){
        g_variant_builder_add(&entry,"{sv}","next-hop",g_variant_new_string(gateway));
        g_variant_builder_add(&entry,"{sv}","onlink",g_variant_new_boolean(TRUE));
    }
    g_variant_builder_add_value(routes,g_variant_builder_end(&entry));
}
/* Cross-device deduplication of projected learned routes within one cycle.
 * Identical (dest,prefix,next-hop,metric) entries are installed once; routes
 * that differ keep their own metrics so the kernel resolves the conflict. */
struct projected_route { unsigned char bytes[16]; unsigned prefix,metric; char gateway[INET6_ADDRSTRLEN]; int family; };
static struct projected_route projected[2u*MAX_ROUTE_ENTRIES];
static unsigned projected_count;
static void projected_reset(void){projected_count=0;}
static int projected_seen(const struct address *a,const char *gateway,unsigned metric,int family)
{
    size_t size=family==AF_INET?4u:16u;
    for(unsigned i=0;i<projected_count;i++){
        const struct projected_route *p=&projected[i];
        if(p->family==family&&p->prefix==a->prefix&&p->metric==metric&&
           !strcmp(p->gateway,gateway?gateway:"")&&!memcmp(p->bytes,a->bytes,size))return 1;
    }
    return 0;
}
static void projected_add(const struct address *a,const char *gateway,unsigned metric,int family)
{
    if(projected_count>=2u*MAX_ROUTE_ENTRIES)return;
    struct projected_route *p=&projected[projected_count++];
    memset(p,0,sizeof *p);
    memcpy(p->bytes,a->bytes,family==AF_INET?4u:16u);
    p->prefix=a->prefix;p->metric=metric;p->family=family;
    if(gateway)memcpy(p->gateway,gateway,strlen(gateway)+1u);
}
static void rule(GVariantBuilder *rules,const struct address *address,int family,unsigned table)
{
    GVariantBuilder entry;g_variant_builder_init(&entry,G_VARIANT_TYPE("a{sv}"));
    g_variant_builder_add(&entry,"{sv}","family",g_variant_new_int32(family));
    g_variant_builder_add(&entry,"{sv}","priority",g_variant_new_uint32(10000u+table));
    g_variant_builder_add(&entry,"{sv}","table",g_variant_new_uint32(table));
    /* The action key stays out: NM normalizes the default (to-table) away in
     * the applied connection, and equivalent() treats the explicit default
     * byte and an absent key as the same rule either way. */
    g_variant_builder_add(&entry,"{sv}","from",g_variant_new_string(address->text));
    g_variant_builder_add(&entry,"{sv}","from-len",g_variant_new_byte(family==AF_INET?32u:128u));
    g_variant_builder_add_value(rules,g_variant_builder_end(&entry));
}
static GVariant *ip_settings(GVariant *applied,GVariant *baseline,const struct ip_state *ip,
                            int selected,int carrier,unsigned table,unsigned metric,int *status)
{
    GVariantBuilder routes,rules;g_variant_builder_init(&routes,G_VARIANT_TYPE("aa{sv}"));g_variant_builder_init(&rules,G_VARIANT_TYPE("aa{sv}"));
    *status=array_copy(&routes,baseline,"route-data");
    if(!*status)*status=array_copy(&rules,baseline,"routing-rules");
    if(*status){g_variant_builder_clear(&routes);g_variant_builder_clear(&rules);return NULL;}
    /* Native 1.42 keyfile serialization exposes modern route-data. Refuse a
     * hand-edited legacy-only route array rather than silently discard it. */
    GVariant *legacy=g_variant_lookup_value(baseline,"routes",NULL);
    GVariant *modern=g_variant_lookup_value(baseline,"route-data",G_VARIANT_TYPE("aa{sv}"));
    if(legacy&&g_variant_n_children(legacy)&&(!modern||!g_variant_n_children(modern)))*status=-EOPNOTSUPP;
    if(legacy)g_variant_unref(legacy);
    if(modern)g_variant_unref(modern);
    if(*status){g_variant_builder_clear(&routes);g_variant_builder_clear(&rules);return NULL;}
    if(carrier){
        for(unsigned j=0;j<ip->count;j++){
            rule(&rules,&ip->addresses[j],ip->family,table);
            route(&routes,&ip->addresses[j],NULL,254u,metric,0,ip->family,ip->addresses[j].text);
        }
        if(selected&&ip->count&&ip->gateway[0])route(&routes,&ip->addresses[0],ip->gateway,254u,metric,1,ip->family,ip->addresses[0].text);
        /* Preserve DHCP-provided more-specific routes in the ordinary
         * destination-route selection of new connections. Their source is
         * this interface's primary address; NM owns their lifetime. */
        const char *source=ip->count?ip->addresses[0].text:NULL;
        for(unsigned j=0;j<ip->learned_count;j++){
            const struct learned_route *entry=&ip->learned[j];
            if(projected_seen(&entry->dest,entry->gateway,entry->metric,ip->family))continue;
            route(&routes,&entry->dest,entry->gateway,254u,entry->metric,0,ip->family,source);
            projected_add(&entry->dest,entry->gateway,entry->metric,ip->family);
        }
    }
    GVariantDict dict;g_variant_dict_init(&dict,applied);
    g_variant_dict_remove(&dict,"routes");
    g_variant_dict_insert_value(&dict,"route-data",g_variant_builder_end(&routes));
    g_variant_dict_insert_value(&dict,"routing-rules",g_variant_builder_end(&rules));
    /* DNS stays under the standard NetworkManager/administrator policy. The
     * previously forced exclusive dns-priority is reset to the profile value
     * instead of being re-imposed each cycle. */
    g_variant_dict_remove(&dict,"dns-priority");
    return g_variant_ref_sink(g_variant_dict_end(&dict));
}
static int fields_same(GVariant *left,GVariant *right)
{
    const char *const keys[]={"route-data","routing-rules","dns-priority"};
    for(unsigned i=0;i<3u;i++){
        GVariant *a=g_variant_lookup_value(left,keys[i],NULL),*b=g_variant_lookup_value(right,keys[i],NULL);
        int same;
        if(i<2u&&(!a||!g_variant_n_children(a))&&(!b||!g_variant_n_children(b)))same=1;
        else same=equivalent(a,b);
        if(a)g_variant_unref(a);
        if(b)g_variant_unref(b);
        if(!same)return 0;
    }
    return 1;
}
struct reapply_backoff {
    uint64_t retry_at;
    unsigned delay_ms;
    int error;
};

/* Prepare both complete requests before changing any routes. */
static int prepare_settings(const struct device *d,int selected,int carrier,GVariant **settings)
{
    *settings=NULL;
    if(!d->active)return 0;
    GVariant *replacement[2]={NULL,NULL};int rc=0,changed=0;
    for(unsigned f=0;f<2u;f++){
        const char *name=f?"ipv6":"ipv4";
        GVariant *a=g_variant_lookup_value(d->applied,name,G_VARIANT_TYPE("a{sv}"));
        GVariant *base=g_variant_lookup_value(d->baseline,name,G_VARIANT_TYPE("a{sv}"));
        if(!a||!base)rc=-EPROTO;
        else replacement[f]=ip_settings(a,base,&d->ip[f],selected,carrier,d->table,d->metric,&rc);
        if(!rc&&!fields_same(a,replacement[f]))changed=1;
        if(a)g_variant_unref(a);
        if(base)g_variant_unref(base);
        if(rc)break;
    }
    if(!rc&&changed){
        GVariantBuilder all;g_variant_builder_init(&all,G_VARIANT_TYPE("a{sa{sv}}"));
        GVariantIter iter;g_variant_iter_init(&iter,d->applied);const char *setting;GVariant *value;
        while(g_variant_iter_next(&iter,"{&s@a{sv}}",&setting,&value)){
            GVariant *next=!strcmp(setting,"ipv4")?replacement[0]:!strcmp(setting,"ipv6")?replacement[1]:value;
            g_variant_builder_add(&all,"{s@a{sv}}",setting,next);g_variant_unref(value);
        }
        *settings=g_variant_ref_sink(g_variant_builder_end(&all));
    }
    for(unsigned f=0;f<2u;f++)if(replacement[f])g_variant_unref(replacement[f]);
    return rc;
}
static int apply_settings(GDBusConnection *bus,const struct device *d,GVariant *settings,
                          struct reapply_backoff *backoff,int *changed)
{
    if(!settings){
        if(d->active)*backoff=(struct reapply_backoff){0};
        return 0;
    }
    if(milliseconds()<backoff->retry_at)return backoff->error;
    *changed=1;
    int rc=0;
    /* NM1.42 preserve-external-ip flag; keep administrator addresses/routes. */
    GVariant *reply=call(bus,d->path,NM_DEVICE,"Reapply",g_variant_new("(@a{sa{sv}}tu)",settings,d->version,1u),G_VARIANT_TYPE("()"),&rc);
    if(reply)g_variant_unref(reply);
    if(!rc)*backoff=(struct reapply_backoff){0};
    else{
        unsigned delay=backoff->delay_ms?2u*backoff->delay_ms:1000u;
        backoff->delay_ms=MIN(delay,30000u);
        backoff->retry_at=milliseconds()+backoff->delay_ms;
        backoff->error=rc;
    }
    return rc;
}
static int main_default(const struct device *d,unsigned family)
{
    if(!d->applied)return 0;
    GVariant *ip=g_variant_lookup_value(d->applied,family?"ipv6":"ipv4",G_VARIANT_TYPE_VARDICT);
    if(!ip)return 0;
    GVariant *routes=g_variant_lookup_value(ip,"route-data",G_VARIANT_TYPE("aa{sv}"));
    g_variant_unref(ip);
    if(!routes)return 0;
    GVariantIter iter;g_variant_iter_init(&iter,routes);
    GVariant *entry;int found=0;
    while(!found&&(entry=g_variant_iter_next_value(&iter))){
        guint32 prefix,table;
        found=g_variant_lookup(entry,"prefix","u",&prefix)&&!prefix&&
              g_variant_lookup(entry,"table","u",&table)&&table==254u;
        g_variant_unref(entry);
    }
    g_variant_unref(routes);
    return found;
}
static int reconcile(GDBusConnection *bus,struct device d[2],const struct y4_network_snapshot *sample,
                     int selected,int collision,struct reapply_backoff backoff[2],int *changed)
{
    if(!bus)return -EHOSTUNREACH;
    for(unsigned r=0;r<2u;r++)
        if(d[r].status&&d[r].status!=-ENODEV)return d[r].status;
    if(selected==Y4_NET_EGRESS_NONE&&!collision)return -EAGAIN;
    unsigned primary=selected==Y4_NET_EGRESS_BOND?1u:0u,other=1u-primary;
    int carrier[2];
    for(unsigned r=0;r<2u;r++)
        carrier[r]=sample->logical[r].link.valid&&sample->logical[r].link.present&&sample->logical[r].link.carrier;
    if(!collision){
        if(sample->address_error||sample->route_error||!d[primary].active||!carrier[primary])return -EAGAIN;
        int ready=0;
        for(unsigned f=0;f<2u;f++){
            int have_gateway=d[primary].ip[f].count&&d[primary].ip[f].gateway[0];
            int old_default=main_default(&d[0],f)||main_default(&d[1],f)||
                            (!f&&(sample->default_egress==Y4_NET_EGRESS_LAN||
                                  sample->default_egress==Y4_NET_EGRESS_BOND));
            if(old_default&&!have_gateway)return -EAGAIN;
            ready|=have_gateway;
        }
        if(!ready)return -EAGAIN;
    }
    GVariant *settings[2]={NULL,NULL};
    projected_reset();
    /* Keep the existing order for deduplication of learned routes. */
    int rc=prepare_settings(&d[other],0,carrier[other]&&!collision,&settings[other]);
    if(rc)d[other].status=rc;
    if(!rc){
        rc=prepare_settings(&d[primary],!collision,carrier[primary]&&!collision,&settings[primary]);
        if(rc)d[primary].status=rc;
    }
    /* Wait for the next GetAppliedConnection to confirm the replacement
     * before withdrawing the previous default. Collisions still withdraw both. */
    if(!rc){
        rc=apply_settings(bus,&d[primary],settings[primary],&backoff[primary],changed);
        if(rc)d[primary].status=rc;
        if((!rc&&!settings[primary])||collision){
            int secondary=apply_settings(bus,&d[other],settings[other],&backoff[other],changed);
            if(secondary)d[other].status=secondary;
            if(!rc)rc=secondary;
        }
    }
    for(unsigned r=0;r<2u;r++)if(settings[r])g_variant_unref(settings[r]);
    return rc;
}
static int selected_egress(const struct y4_network_snapshot *s)
{
    for(unsigned i=Y4_NET_SFP1;i<=Y4_NET_SFP2;i++)if(s->physical[i].valid&&s->physical[i].present&&!s->physical[i].error&&s->physical[i].carrier)return Y4_NET_EGRESS_BOND;
    for(unsigned i=Y4_NET_SFP1;i<=Y4_NET_SFP2;i++)if(!s->physical[i].valid||s->physical[i].error)return Y4_NET_EGRESS_NONE;
    return Y4_NET_EGRESS_LAN;
}
static int duplicate_ip(const struct device *a,const struct device *b)
{
    if(!a->active||!b->active)return 0;
    for(unsigned f=0;f<2u;f++)for(unsigned i=0;i<a->ip[f].count;i++)for(unsigned j=0;j<b->ip[f].count;j++)
        if(!memcmp(a->ip[f].addresses[i].bytes,b->ip[f].addresses[j].bytes,f?16u:4u))return 1;
    return 0;
}
static int status_store(int directory,uint64_t now,int selected,int error,int lan,int bond,int actual,int changed)
{
    char text[512];int n=snprintf(text,sizeof text,"{\"schema\":1,\"sampled_ms\":%llu,\"selected_egress\":%d,\"status\":%d,\"lan_status\":%d,\"bond_status\":%d,\"observed_ipv4_egress\":%d,\"reapply_changed\":%s,\"observed_before_reapply\":true}\n",
                (unsigned long long)now,selected,error,lan,bond,actual,changed?"true":"false");
    if(n<0||(size_t)n>=sizeof text)return -EOVERFLOW;
    char name[64];(void)snprintf(name,sizeof name,".network-policy-%ld",(long)getpid());
    int fd=openat(directory,name,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,0600);if(fd<0)return -errno;
    int rc=0;size_t at=0;
    while(at<(size_t)n){
        ssize_t wrote=write(fd,text+at,(size_t)n-at);
        if(wrote<0&&errno==EINTR)continue;
        if(wrote<=0){rc=wrote<0?-errno:-EIO;break;}
        at+=(size_t)wrote;
    }
    if(close(fd)&&!rc)rc=-errno;
    if(!rc&&renameat(directory,name,directory,"network-policy.json"))rc=-errno;
    if(rc)(void)unlinkat(directory,name,0);
    return rc;
}
int main(int argc,char **argv)
{
    (void)argv;if(argc!=1||geteuid()){fprintf(stderr,"y4-network-policy requires root and no arguments\n");return 2;}
    int dir=open("/run/y4-board",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);struct stat st;
    if(dir<0||fstat(dir,&st)||st.st_uid||!S_ISDIR(st.st_mode)||(st.st_mode&07777u)!=0700u){fprintf(stderr,"invalid board runtime directory\n");if(dir>=0)close(dir);return 1;}
    int lock=openat(dir,"network-policy.lock",O_RDWR|O_CREAT|O_NOFOLLOW|O_CLOEXEC,0600);
    if(lock<0||fstat(lock,&st)||!S_ISREG(st.st_mode)||st.st_uid||st.st_nlink!=1u||
       (st.st_mode&07777u)!=0600u||flock(lock,LOCK_EX|LOCK_NB)){
        if(lock>=0)close(lock);
        close(dir);return 1;
    }
    struct sigaction sa={0};sa.sa_handler=stop;sigemptyset(&sa.sa_mask);sigaction(SIGTERM,&sa,NULL);sigaction(SIGINT,&sa,NULL);
    struct reapply_backoff backoff[2]={{0}};
    GDBusConnection *bus=NULL;int prior_status=INT_MAX,prior_selected=-1;uint64_t next_log=0;
    while(!stopping){
        if(!bus||g_dbus_connection_is_closed(bus)){
            if(bus)g_object_unref(bus);
            GError *error=NULL;bus=g_bus_get_sync(G_BUS_TYPE_SYSTEM,NULL,&error);
            if(error){fprintf(stderr,"y4-network-policy: system D-Bus unavailable: %s\n",error->message);g_error_free(error);}
        }
        struct y4_network_snapshot sample;int query=y4_network_sample(&sample);
        int selected=selected_egress(&sample);
        if(query&&!sample.address_error&&!sample.route_error)selected=Y4_NET_EGRESS_NONE;
        struct device d[2]={{0}};
        for(unsigned r=0;r<2u;r++)if(bus)d[r].status=load_device(bus,r,&d[r]);
        int collision=duplicate_ip(&d[0],&d[1]);
        if(collision)selected=Y4_NET_EGRESS_NONE;
        int changed=0;
        int rc=reconcile(bus,d,&sample,selected,collision,backoff,&changed);
        if(collision)rc=-EADDRINUSE;
        else if(!rc&&(d[0].status||d[1].status))rc=d[0].status?d[0].status:d[1].status;
        uint64_t now=milliseconds();
        int saved=status_store(dir,sample.sampled_ms,selected,rc,d[0].status,d[1].status,(int)sample.default_egress,changed);
        if(saved&&!rc)rc=saved;
        if(rc!=prior_status||selected!=prior_selected||now>=next_log){
            fprintf(stderr,"y4-network-policy: requested=%d kernel_before=%d status=%d lan=%d bond=%d changed=%d\n",selected,sample.default_egress,rc,d[0].status,d[1].status,changed);
            prior_status=rc;prior_selected=selected;next_log=now+60000u;
        }
        for(unsigned r=0;r<2u;r++)device_clear(&d[r]);
        for(unsigned i=0;i<5u&&!stopping;i++){while(g_main_context_iteration(NULL,FALSE)){}struct timespec t={0,100000000};(void)nanosleep(&t,NULL);}
    }
    if(bus)g_object_unref(bus);
    close(lock);close(dir);return 0;
}
