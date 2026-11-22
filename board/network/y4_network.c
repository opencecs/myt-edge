/* SPDX-License-Identifier: Apache-2.0 */
/* Bounded read-only rtnetlink collector. No exec or network configuration. */
#define _GNU_SOURCE
#include "y4_network.h"
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/if_addr.h>
#include <linux/if_link.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define SAMPLE_BUDGET_MS 750u
#define LINK_LIMIT 256u
#define ATTR_TYPE(a) ((a)->rta_type & NLA_TYPE_MASK)
struct kernel_link {
    unsigned index,master,carrier,mode,agg,agg_ports,partner_key;
    unsigned slave_state,slave_agg,actor_state,partner_state;
    int carrier_valid,is_bond,ad_valid,slave_valid,slave_state_valid,mac_valid;
    unsigned char mac[6],partner_mac[6];char name[IF_NAMESIZE];
};
struct sample {
    struct y4_network_snapshot *out;
    struct kernel_link links[LINK_LIMIT];
    unsigned link_count,route_count;
    int ipv4_secondary[Y4_NET_LOGICAL_COUNT];
    uint32_t role_gw[Y4_NET_LOGICAL_COUNT];
    unsigned role_gw_metric[Y4_NET_LOGICAL_COUNT];
    uint8_t role_gw_valid[Y4_NET_LOGICAL_COUNT];
    uint64_t deadline;
};
static uint64_t now_ms(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC,&t) || t.tv_sec<0) return 0;
    return (uint64_t)t.tv_sec*1000u+(uint64_t)t.tv_nsec/1000000u;
}
static uint64_t boot_ms(void)
{
    struct timespec t;
    if(clock_gettime(CLOCK_BOOTTIME,&t)||t.tv_sec<0)return 0;
    return (uint64_t)t.tv_sec*1000u+(uint64_t)t.tv_nsec/1000000u;
}
static int read_number(const char *path,unsigned *out)
{
    int fd=open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);
    if(fd<0)return -errno;
    char text[48];ssize_t n;
    do {n=read(fd,text,sizeof(text)-1u);} while(n<0&&errno==EINTR);
    int rc=n<0?-errno:0;
    if(close(fd)&&!rc)rc=-errno;
    if(rc)return rc;
    if(n<1||n==(ssize_t)(sizeof(text)-1u))return -EPROTO;
    text[n]=0;char *end=NULL;errno=0;
    unsigned long v=strtoul(text,&end,0);
    if(errno||v>UINT_MAX||end==text)return -EPROTO;
    if(*end=='\n')end++;
    if(*end)return -EPROTO;
    *out=(unsigned)v;return 0;
}
/* 64-bit kernel statistics counter (one small sysfs file, no exec). */
static int read_counter(const char *path,uint64_t *out)
{
    int fd=open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);
    if(fd<0)return -errno;
    char text[48];ssize_t n;
    do {n=read(fd,text,sizeof(text)-1u);} while(n<0&&errno==EINTR);
    int rc=n<0?-errno:0;
    if(close(fd)&&!rc)rc=-errno;
    if(rc)return rc;
    if(n<1||n==(ssize_t)(sizeof(text)-1u))return -EPROTO;
    text[n]=0;char *end=NULL;errno=0;
    unsigned long long v=strtoull(text,&end,0);
    if(errno||end==text)return -EPROTO;
    if(*end=='\n')end++;
    if(*end)return -EPROTO;
    *out=(uint64_t)v;return 0;
}
/* rx/tx bytes+packets for one interface; failure keeps old values and
 * only marks counters_error (never invalidates the link observation). */
static void read_counters(struct y4_network_link *link)
{
    char path[160];
    int n=snprintf(path,sizeof path,"/sys/class/net/%s/statistics/rx_bytes",link->name);
    if(n<0||(size_t)n>=sizeof path){link->counters_error=-ENAMETOOLONG;return;}
    int rc=read_counter(path,&link->rx_bytes);
    if(!rc){
        snprintf(path,sizeof path,"/sys/class/net/%s/statistics/tx_bytes",link->name);
        rc=read_counter(path,&link->tx_bytes);
    }
    if(!rc){
        snprintf(path,sizeof path,"/sys/class/net/%s/statistics/rx_packets",link->name);
        rc=read_counter(path,&link->rx_packets);
    }
    if(!rc){
        snprintf(path,sizeof path,"/sys/class/net/%s/statistics/tx_packets",link->name);
        rc=read_counter(path,&link->tx_packets);
    }
    link->counters_error=rc;
}
static int string_attr(const struct rtattr *a,char *out,size_t cap)
{
    size_t n=RTA_PAYLOAD(a);
    const char *p=RTA_DATA(a);
    if(!n||n>cap||p[n-1u]||strnlen(p,n)!=n-1u)return -EPROTO;
    memcpy(out,p,n);return 0;
}
static int scalar(const struct rtattr *a,unsigned *out,unsigned bytes)
{
    if(RTA_PAYLOAD(a)!=bytes)return -EPROTO;
    if(bytes==1u)*out=*(const uint8_t *)RTA_DATA(a);
    else if(bytes==2u){uint16_t value;memcpy(&value,RTA_DATA(a),sizeof value);*out=value;}
    else if(bytes==4u){uint32_t value;memcpy(&value,RTA_DATA(a),sizeof value);*out=value;}
    else return -EINVAL;
    return 0;
}
static int nested_bond(const struct rtattr *info,struct kernel_link *link)
{
    unsigned len=(unsigned)RTA_PAYLOAD(info);const struct rtattr *a=RTA_DATA(info),*data=NULL,*slave_data=NULL;
    char kind[32]={0},slave_kind[32]={0};
    for(;RTA_OK(a,len);a=RTA_NEXT(a,len)){
        if(ATTR_TYPE(a)==IFLA_INFO_KIND){int rc=string_attr(a,kind,sizeof kind);if(rc)return rc;}
        else if(ATTR_TYPE(a)==IFLA_INFO_DATA)data=a;
        else if(ATTR_TYPE(a)==IFLA_INFO_SLAVE_KIND){int rc=string_attr(a,slave_kind,sizeof slave_kind);if(rc)return rc;}
        else if(ATTR_TYPE(a)==IFLA_INFO_SLAVE_DATA)slave_data=a;
    }
    if(len)return -EPROTO;
    link->is_bond=!strcmp(kind,"bond");
    if(link->is_bond&&data){
        len=(unsigned)RTA_PAYLOAD(data);a=RTA_DATA(data);
        for(;RTA_OK(a,len);a=RTA_NEXT(a,len)){
            if(ATTR_TYPE(a)==IFLA_BOND_MODE){int rc=scalar(a,&link->mode,1);if(rc)return rc;}
            else if(ATTR_TYPE(a)==IFLA_BOND_AD_INFO){
                unsigned n=(unsigned)RTA_PAYLOAD(a),seen=0;const struct rtattr *b=RTA_DATA(a);
                for(;RTA_OK(b,n);b=RTA_NEXT(b,n)){
                    int rc=0;
                    if(ATTR_TYPE(b)==IFLA_BOND_AD_INFO_AGGREGATOR){rc=scalar(b,&link->agg,2);seen|=1u;}
                    else if(ATTR_TYPE(b)==IFLA_BOND_AD_INFO_NUM_PORTS){rc=scalar(b,&link->agg_ports,2);seen|=2u;}
                    else if(ATTR_TYPE(b)==IFLA_BOND_AD_INFO_PARTNER_KEY){rc=scalar(b,&link->partner_key,2);seen|=4u;}
                    else if(ATTR_TYPE(b)==IFLA_BOND_AD_INFO_PARTNER_MAC){
                        if(RTA_PAYLOAD(b)!=6)return -EPROTO;
                        memcpy(link->partner_mac,RTA_DATA(b),6);seen|=8u;
                    }
                    if(rc)return rc;
                }
                if(n)return -EPROTO;
                link->ad_valid=seen==15u;
            }
        }
        if(len)return -EPROTO;
    }
    if(!strcmp(slave_kind,"bond")&&slave_data){
        len=(unsigned)RTA_PAYLOAD(slave_data);a=RTA_DATA(slave_data);unsigned seen=0;
        for(;RTA_OK(a,len);a=RTA_NEXT(a,len)){
            int rc=0;
            if(ATTR_TYPE(a)==IFLA_BOND_SLAVE_STATE){rc=scalar(a,&link->slave_state,1);seen|=1u;}
            else if(ATTR_TYPE(a)==IFLA_BOND_SLAVE_AD_AGGREGATOR_ID){rc=scalar(a,&link->slave_agg,2);seen|=2u;}
            else if(ATTR_TYPE(a)==IFLA_BOND_SLAVE_AD_ACTOR_OPER_PORT_STATE){rc=scalar(a,&link->actor_state,1);seen|=4u;}
            else if(ATTR_TYPE(a)==IFLA_BOND_SLAVE_AD_PARTNER_OPER_PORT_STATE){
                unsigned bytes=(unsigned)RTA_PAYLOAD(a);
                if(bytes!=1u&&bytes!=2u)return -EPROTO;
                rc=scalar(a,&link->partner_state,bytes);if(link->partner_state>255u)return -EPROTO;seen|=8u;
            }
            if(rc)return rc;
        }
        if(len)return -EPROTO;
        link->slave_valid=seen==15u;
        link->slave_state_valid=!!(seen&1u);
    }
    return 0;
}
static int link_message(struct sample *s,const struct nlmsghdr *h)
{
    if(h->nlmsg_type!=RTM_NEWLINK||h->nlmsg_len<NLMSG_LENGTH(sizeof(struct ifinfomsg)))return -EPROTO;
    if(s->link_count==LINK_LIMIT)return -E2BIG;
    const struct ifinfomsg *i=NLMSG_DATA(h);
    if(i->ifi_index<=0)return -EPROTO;
    struct kernel_link next={.index=(unsigned)i->ifi_index,.carrier=!!(i->ifi_flags&0x10000u)};
    unsigned len=(unsigned)(h->nlmsg_len-NLMSG_LENGTH(sizeof *i));
    const struct rtattr *a=IFLA_RTA(i);
    for(;RTA_OK(a,len);a=RTA_NEXT(a,len)){
        if(ATTR_TYPE(a)==IFLA_IFNAME){int rc=string_attr(a,next.name,sizeof next.name);if(rc)return rc;}
        else if(ATTR_TYPE(a)==IFLA_CARRIER){if(RTA_PAYLOAD(a)!=1)return -EPROTO;next.carrier=!!*(const unsigned char *)RTA_DATA(a);next.carrier_valid=1;}
        else if(ATTR_TYPE(a)==IFLA_MASTER){if(RTA_PAYLOAD(a)!=4)return -EPROTO;memcpy(&next.master,RTA_DATA(a),4);}
        else if(ATTR_TYPE(a)==IFLA_ADDRESS){if(RTA_PAYLOAD(a)!=6)return -EPROTO;memcpy(next.mac,RTA_DATA(a),6);next.mac_valid=1;}
#ifdef IFLA_PERM_ADDRESS
        else if(ATTR_TYPE(a)==IFLA_PERM_ADDRESS){if(RTA_PAYLOAD(a)!=6)return -EPROTO;memcpy(next.mac,RTA_DATA(a),6);next.mac_valid=2;}
#endif
        else if(ATTR_TYPE(a)==IFLA_LINKINFO){int rc=nested_bond(a,&next);if(rc)return rc;}
    }
    if(len||!next.name[0])return -EPROTO;
    for(unsigned j=0;j<s->link_count;j++)if(s->links[j].index==next.index)return -EPROTO;
    s->links[s->link_count++]=next;return 0;
}
static int physical_role(const char *name,int *identity_error)
{
    *identity_error=0;
    static const char *const ancestry[]={"/a0e0000.pcie/","/a0d0000.pcie/","/a0c0000.pcie/"};
    char path[PATH_MAX],real[PATH_MAX];
    int n=snprintf(path,sizeof path,"/sys/class/net/%s/device",name);
    if(n<0||(size_t)n>=sizeof path)return -ENAMETOOLONG;
    if(!realpath(path,real))return errno==ENOENT?-ENOENT:-errno;
    int role=-1;
    for(unsigned j=0;j<Y4_NET_PHYSICAL_COUNT;j++)if(strstr(real,ancestry[j])){if(role>=0)return -ENOTUNIQ;role=(int)j;}
    if(role<0)return -ENOENT;
    unsigned vendor,device;
    n=snprintf(path,sizeof path,"%s/vendor",real);
    if(n<0||(size_t)n>=sizeof path)return -ENAMETOOLONG;
    int rc=read_number(path,&vendor);if(rc){*identity_error=rc;return role;}
    n=snprintf(path,sizeof path,"%s/device",real);
    if(n<0||(size_t)n>=sizeof path)return -ENAMETOOLONG;
    rc=read_number(path,&device);if(rc){*identity_error=rc;return role;}
    if(vendor!=0x10ecu||device!=(role==Y4_NET_LAN?0x8125u:0x8127u))*identity_error=-ENODEV;
    return role;
}
static void assign_link(struct y4_network_link *out,const struct kernel_link *link)
{
    *out=(struct y4_network_link){.valid=1,.present=1,.carrier=(int)link->carrier,.ifindex=link->index,.master_ifindex=link->master};
    if(link->mac_valid){memcpy(out->mac,link->mac,6);out->mac_valid=1;}
    memcpy(out->name,link->name,sizeof out->name);
    char path[128];unsigned speed=0,index=0;
    int n=snprintf(path,sizeof path,"/sys/class/net/%s/ifindex",link->name);
    int identity=n<0||(size_t)n>=sizeof path?-ENAMETOOLONG:read_number(path,&index);
    if(identity||index!=link->index){out->valid=0;out->error=identity?identity:-ESTALE;return;}
    n=snprintf(path,sizeof path,"/sys/class/net/%s/speed",link->name);
    if(n>0&&(size_t)n<sizeof path&&!read_number(path,&speed)&&speed!=UINT_MAX)out->speed_mbps=speed;
    read_counters(out);
}
static int map_links(struct sample *s)
{
    unsigned seen=0;unsigned bonds=0;int unknown_identity=0;
    for(unsigned i=0;i<Y4_NET_PHYSICAL_COUNT;i++)s->out->physical[i]=(struct y4_network_link){.valid=1};
    s->out->logical[Y4_NET_LOGICAL_BOND].link.valid=1;
    for(unsigned j=0;j<s->link_count;j++){
        const struct kernel_link *link=&s->links[j];
        int identity_error=0;int role=physical_role(link->name,&identity_error);
        if(role>=0){
            if(seen&(1u<<(unsigned)role)){
                s->out->physical[role].valid=0;s->out->physical[role].error=-ENOTUNIQ;
             }else {
                assign_link(&s->out->physical[role],link);seen|=1u<<(unsigned)role;
                if(identity_error){s->out->physical[role].valid=0;s->out->physical[role].error=identity_error;}
            }
        }
        if(role<0&&role!=-ENOENT&&!unknown_identity)unknown_identity=role;
        /* Unrelated netdevs without an expected PCI ancestry are outside this
         * board snapshot. Expected-slot identity errors stay on that slot,
         * preserving observations of the other healthy logical interface. */
        if(!strcmp(link->name,Y4_NET_BOND_NAME)){
            if(++bonds>1u||!link->is_bond){s->out->logical[1].link.error=-EPROTO;s->out->logical[1].link.valid=0;}
            else {assign_link(&s->out->logical[1].link,link);s->out->bond_mode=link->mode;}
        }
    }
    if(unknown_identity)for(unsigned i=0;i<Y4_NET_PHYSICAL_COUNT;i++)if(!(seen&(1u<<i))){
        s->out->physical[i].valid=0;s->out->physical[i].error=unknown_identity;
    }
    s->out->logical[Y4_NET_LOGICAL_LAN].link=s->out->physical[Y4_NET_LAN];
    if(s->out->physical[0].master_ifindex){
        s->out->physical[0].valid=0;s->out->physical[0].error=-EBUSY;
        s->out->logical[0].link=s->out->physical[0];
    }
    unsigned bond=s->out->logical[1].link.ifindex;
    for(unsigned i=Y4_NET_SFP1;i<=Y4_NET_SFP2;i++){
        const struct y4_network_link *p=&s->out->physical[i];
        if(p->master_ifindex&&p->master_ifindex!=bond){
            s->out->physical[i].valid=0;s->out->physical[i].error=-EBUSY;
        }
    }
    return 0;
}
static void map_bond_state(struct sample *s)
{
    struct y4_network_snapshot *out=s->out;
    const struct y4_network_link *bond=&out->logical[Y4_NET_LOGICAL_BOND].link;
    out->bond_state=Y4_NET_BOND_UNKNOWN;
    out->bond_state_error=-ENODATA;
    if(!bond->present){out->bond_state=Y4_NET_BOND_DOWN;out->bond_state_error=0;return;}
    const struct kernel_link *master=NULL;
    for(unsigned i=0;i<s->link_count;i++)if(s->links[i].index==bond->ifindex)master=&s->links[i];
    if(!master)return;
    if(master->ad_valid){
        out->bond_ad_info_valid=1;out->bond_aggregator_id=master->agg;out->bond_aggregator_ports=master->agg_ports;
        out->bond_partner_key=master->partner_key;memcpy(out->bond_partner_mac,master->partner_mac,6);
        for(unsigned i=0;i<6u;i++)out->bond_partner_present|=master->partner_mac[i]!=0;
    }
    unsigned member_seen=0,member_valid=0;
    for(unsigned role=Y4_NET_SFP1;role<=Y4_NET_SFP2;role++){
        const struct y4_network_link *physical=&out->physical[role];
        if(!physical->valid||physical->error||!physical->present||physical->master_ifindex!=bond->ifindex||!physical->carrier)continue;
        member_seen|=1u<<role;
        for(unsigned j=0;j<s->link_count;j++){
            const struct kernel_link *port=&s->links[j];
            if(port->index!=physical->ifindex)continue;
            if(master->mode==4u){
                if(!master->ad_valid||!port->slave_valid)continue;
                member_valid|=1u<<role;
                if(port->slave_agg!=master->agg)continue;
                if(port->slave_state==0u)out->bond_active_mask|=1u<<role;
                if((port->actor_state&0x30u)==0x30u&&(port->partner_state&0x30u)==0x30u)
                    out->bond_lacp_mask|=1u<<role;
            }else if(master->mode==1u&&port->slave_state_valid){
                member_valid|=1u<<role;
                if(port->slave_state==0u)out->bond_active_mask|=1u<<role;
            }
        }
    }
    if(!member_seen){out->bond_state=Y4_NET_BOND_DOWN;out->bond_state_error=0;return;}
    if(member_valid!=member_seen)return;
    if(master->mode==4u&&master->ad_valid){
        out->bond_state_error=0;
        if(out->bond_partner_present){
            if((out->bond_lacp_mask&6u)==6u)out->bond_state=Y4_NET_BOND_LACP_MULTI;
            else if(out->bond_lacp_mask&6u)out->bond_state=Y4_NET_BOND_LACP_SINGLE;
            else out->bond_state=Y4_NET_BOND_NEGOTIATING;
        }else if(out->bond_active_mask&&!(out->bond_active_mask&(out->bond_active_mask-1u))&&master->agg_ports==1u&&bond->carrier){
            out->bond_state=Y4_NET_BOND_INDIVIDUAL;
        }else out->bond_state=Y4_NET_BOND_NEGOTIATING;
    }else if(master->mode==1u&&out->bond_active_mask){
        out->bond_state=Y4_NET_BOND_INDIVIDUAL;out->bond_state_error=0;
    }
}
static int address_message(struct sample *s,const struct nlmsghdr *h)
{
    if(h->nlmsg_type!=RTM_NEWADDR||h->nlmsg_len<NLMSG_LENGTH(sizeof(struct ifaddrmsg)))return -EPROTO;
    const struct ifaddrmsg *i=NLMSG_DATA(h);
    if(i->ifa_family!=AF_INET)return 0;
    if(i->ifa_prefixlen>32)return -EPROTO;
    unsigned which=Y4_NET_LOGICAL_COUNT;
    for(unsigned j=0;j<Y4_NET_LOGICAL_COUNT;j++)if(s->out->logical[j].link.valid&&!s->out->logical[j].link.error&&s->out->logical[j].link.present&&s->out->logical[j].link.ifindex==i->ifa_index){which=j;break;}
    if(which==Y4_NET_LOGICAL_COUNT)return 0;
    struct in_addr local={0},address={0};int has_local=0,has_address=0;
    unsigned flags=i->ifa_flags;int preferred=1;
    unsigned len=(unsigned)(h->nlmsg_len-NLMSG_LENGTH(sizeof *i));const struct rtattr *a=IFA_RTA(i);
    for(;RTA_OK(a,len);a=RTA_NEXT(a,len)){
        if(ATTR_TYPE(a)==IFA_LOCAL||ATTR_TYPE(a)==IFA_ADDRESS){
            if(RTA_PAYLOAD(a)!=4)return -EPROTO;
            if(ATTR_TYPE(a)==IFA_LOCAL){memcpy(&local,RTA_DATA(a),4);has_local=1;}
            else{memcpy(&address,RTA_DATA(a),4);has_address=1;}
        }else if(ATTR_TYPE(a)==IFA_FLAGS){if(RTA_PAYLOAD(a)!=4)return -EPROTO;memcpy(&flags,RTA_DATA(a),4);}
        else if(ATTR_TYPE(a)==IFA_CACHEINFO){if(RTA_PAYLOAD(a)<sizeof(struct ifa_cacheinfo))return -EPROTO;struct ifa_cacheinfo cache;memcpy(&cache,RTA_DATA(a),sizeof cache);preferred=cache.ifa_prefered!=0;}
    }
    if(len)return -EPROTO;
    if(!has_local){if(!has_address)return -EPROTO;local=address;}
    uint32_t host=ntohl(local.s_addr);
    if((flags&(IFA_F_TENTATIVE|IFA_F_DADFAILED))||!preferred||i->ifa_scope>RT_SCOPE_LINK||!host||(host>>24)==127u||(host>>28)==0xeu)return 0;
    int secondary=!!(flags&IFA_F_SECONDARY);
    struct y4_network_interface *out=&s->out->logical[which];
    if(out->ipv4_valid&&(!s->ipv4_secondary[which]||secondary))return 0;
    if(!inet_ntop(AF_INET,&local,out->ipv4,sizeof out->ipv4))return -errno;
    out->ipv4_valid=1;out->prefix_length=i->ifa_prefixlen;s->ipv4_secondary[which]=secondary;return 0;
}
static unsigned route_role_mask(const struct sample *s,unsigned index)
{
    if(index&&index==s->out->logical[0].link.ifindex)return 1u;
    if(index&&index==s->out->logical[1].link.ifindex)return 2u;
    return 4u;
}
static void consider_route(struct sample *s,unsigned index,unsigned metric,unsigned mask)
{
    if(!s->route_count||metric<s->out->default_metric){
        s->out->default_metric=metric;s->out->default_ifindex=index;s->out->default_mask=mask;s->route_count=1;
    }else if(metric==s->out->default_metric){
        if(index!=s->out->default_ifindex)s->out->default_ifindex=0;
        s->out->default_mask|=mask;s->route_count++;
    }
}
static int route_message(struct sample *s,const struct nlmsghdr *h)
{
    if(h->nlmsg_type!=RTM_NEWROUTE||h->nlmsg_len<NLMSG_LENGTH(sizeof(struct rtmsg)))return -EPROTO;
    const struct rtmsg *r=NLMSG_DATA(h);
    if(r->rtm_family!=AF_INET||r->rtm_dst_len||r->rtm_src_len||r->rtm_type!=RTN_UNICAST||(r->rtm_flags&RTM_F_CLONED))return 0;
    unsigned index=0,table=r->rtm_table,metric=0;const struct rtattr *multipath=NULL;
    uint32_t gw=0;
    unsigned len=(unsigned)(h->nlmsg_len-NLMSG_LENGTH(sizeof *r));const struct rtattr *a=RTM_RTA(r);
    for(;RTA_OK(a,len);a=RTA_NEXT(a,len)){
        unsigned *target=NULL;
        if(ATTR_TYPE(a)==RTA_TABLE)target=&table;
        else if(ATTR_TYPE(a)==RTA_OIF)target=&index;
        else if(ATTR_TYPE(a)==RTA_PRIORITY)target=&metric;
        else if(ATTR_TYPE(a)==RTA_MULTIPATH)multipath=a;
        else if(ATTR_TYPE(a)==RTA_DST){if(RTA_PAYLOAD(a)!=4)return -EPROTO;uint32_t dst;memcpy(&dst,RTA_DATA(a),4);if(dst)return -EPROTO;}
        else if(ATTR_TYPE(a)==RTA_GATEWAY){if(RTA_PAYLOAD(a)!=4)return -EPROTO;memcpy(&gw,RTA_DATA(a),4);}
        if(target){if(RTA_PAYLOAD(a)!=4)return -EPROTO;memcpy(target,RTA_DATA(a),4);}
    }
    if(len)return -EPROTO;
    if(table!=RT_TABLE_MAIN)return 0;
    if(multipath){
        int n=(int)RTA_PAYLOAD(multipath);const struct rtnexthop *hop=RTA_DATA(multipath);
        while(RTNH_OK(hop,n)){
            if(!(hop->rtnh_flags&RTNH_F_DEAD)&&hop->rtnh_ifindex>0){unsigned v=(unsigned)hop->rtnh_ifindex;consider_route(s,v,metric,route_role_mask(s,v));}
            n-=RTNH_ALIGN(hop->rtnh_len);hop=RTNH_NEXT(hop);
        }
        return n?-EPROTO:0;
    }
    if(!index)return -EPROTO;
    /* Default-route gateway per role: the lowest-metric default route's
     * gateway is recorded for the management API (single-path routes;
     * multipath gateways live inside the rtnexthop list and are not
     * attributed here). */
    if(gw){unsigned role=route_role_mask(s,index);
     for(unsigned i=0;i<Y4_NET_LOGICAL_COUNT;i++)
      if(role&(1u<<i)&&(!s->role_gw_valid[i]||metric<s->role_gw_metric[i])){
       s->role_gw[i]=gw;s->role_gw_metric[i]=metric;s->role_gw_valid[i]=1;}
      }
    consider_route(s,index,metric,route_role_mask(s,index));return 0;
}
static int dump(int fd,unsigned sequence,unsigned type,struct sample *s,int (*accept)(struct sample *,const struct nlmsghdr *))
{
    struct {
        struct nlmsghdr h;
        union {struct ifinfomsg link;struct ifaddrmsg address;struct rtmsg route;} payload;
    } request={.h={.nlmsg_type=(uint16_t)type,.nlmsg_flags=NLM_F_REQUEST|NLM_F_DUMP,.nlmsg_seq=sequence}};
    if(type==RTM_GETLINK){request.h.nlmsg_len=NLMSG_LENGTH(sizeof request.payload.link);request.payload.link.ifi_family=AF_UNSPEC;}
    else if(type==RTM_GETADDR){request.h.nlmsg_len=NLMSG_LENGTH(sizeof request.payload.address);request.payload.address.ifa_family=AF_INET;}
    else {request.h.nlmsg_len=NLMSG_LENGTH(sizeof request.payload.route);request.payload.route.rtm_family=AF_INET;}
    struct sockaddr_nl kernel={.nl_family=AF_NETLINK};
    ssize_t n=sendto(fd,&request,request.h.nlmsg_len,0,(struct sockaddr *)&kernel,sizeof kernel);
    if(n<0)return -errno;
    if((size_t)n!=request.h.nlmsg_len)return -EIO;
    for(;;){
        uint64_t now=now_ms();if(!now||now>=s->deadline)return -ETIMEDOUT;
        struct pollfd pollfd={.fd=fd,.events=POLLIN};int wait=(int)(s->deadline-now);
        int polled=poll(&pollfd,1,wait);
        if(polled<0){if(errno==EINTR)continue;return -errno;}
        if(!polled)return -ETIMEDOUT;
        if(!(pollfd.revents&POLLIN))return -EIO;
        union {struct nlmsghdr align;unsigned char bytes[65536];} buffer;struct sockaddr_nl sender={0};struct iovec iov={.iov_base=buffer.bytes,.iov_len=sizeof buffer.bytes};
        struct msghdr msg={.msg_name=&sender,.msg_namelen=sizeof sender,.msg_iov=&iov,.msg_iovlen=1};
        n=recvmsg(fd,&msg,0);
        if(n<0){if(errno==EINTR||errno==EAGAIN)continue;return -errno;}
        if(!n||msg.msg_flags&(MSG_TRUNC|MSG_CTRUNC)||msg.msg_namelen!=sizeof sender||sender.nl_pid)return -EPROTO;
        unsigned left=(unsigned)n;const struct nlmsghdr *h=(const struct nlmsghdr *)buffer.bytes;
        for(;NLMSG_OK(h,left);h=NLMSG_NEXT(h,left)){
            if(h->nlmsg_seq!=sequence)return -EPROTO;
            if(h->nlmsg_flags&NLM_F_DUMP_INTR)return -EAGAIN;
            if(h->nlmsg_type==NLMSG_DONE){
                size_t bytes=NLMSG_PAYLOAD(h,0);
                if(bytes>=sizeof(int)){int status;memcpy(&status,NLMSG_DATA(h),sizeof status);if(status)return status<0?status:-EPROTO;}
                return 0;
            }
            if(h->nlmsg_type==NLMSG_ERROR){
                if(h->nlmsg_len<NLMSG_LENGTH(sizeof(struct nlmsgerr)))return -EPROTO;
                const struct nlmsgerr *e=NLMSG_DATA(h);if(e->error)return e->error<0?e->error:-EPROTO;continue;
            }
            int rc=accept(s,h);if(rc)return rc;
        }
        if(left)return -EPROTO;
    }
}
int y4_network_sample(struct y4_network_snapshot *out)
{
    if(!out)return -EINVAL;
    memset(out,0,sizeof *out);out->sampled_ms=boot_ms();
    if(!out->sampled_ms){out->error=-EIO;return out->error;}
    uint64_t began=now_ms();
    if(!began){out->error=-EIO;return out->error;}
    struct sample s={.out=out,.deadline=began+SAMPLE_BUDGET_MS,.role_gw={0},.role_gw_metric={0},.role_gw_valid={0}};
    int fd=socket(AF_NETLINK,SOCK_RAW|SOCK_CLOEXEC|SOCK_NONBLOCK,NETLINK_ROUTE);
    if(fd<0){out->error=-errno;return out->error;}
    struct sockaddr_nl local={.nl_family=AF_NETLINK};
    int rc=bind(fd,(struct sockaddr *)&local,sizeof local)?-errno:0;
    if(!rc)rc=dump(fd,1,RTM_GETLINK,&s,link_message);
    if(!rc)rc=map_links(&s);
    if(rc){out->error=rc;close(fd);return rc;}
    map_bond_state(&s);
    for(unsigned i=0;i<Y4_NET_LOGICAL_COUNT;i++)if(!out->logical[i].link.valid||out->logical[i].link.error)
        out->logical[i].ipv4_error=out->logical[i].link.error?out->logical[i].link.error:-ENODATA;
    int address=dump(fd,2,RTM_GETADDR,&s,address_message);
    out->address_error=address;
    if(address)for(unsigned j=0;j<Y4_NET_LOGICAL_COUNT;j++){out->logical[j].ipv4_valid=0;out->logical[j].ipv4_error=address;memset(out->logical[j].ipv4,0,sizeof out->logical[j].ipv4);}
    for(unsigned j=0;j<Y4_NET_LOGICAL_COUNT;j++){out->logical[j].gateway_valid=0;memset(out->logical[j].gateway,0,sizeof out->logical[j].gateway);}
    int route=address?address:dump(fd,3,RTM_GETROUTE,&s,route_message);
    out->route_error=route;
    for(unsigned i=0;i<Y4_NET_LOGICAL_COUNT;i++)
     if(!route&&s.role_gw_valid[i]){
      struct y4_network_interface*l=&out->logical[i];
      snprintf(l->gateway,sizeof l->gateway,"%u.%u.%u.%u",
       s.role_gw[i]&255u,(s.role_gw[i]>>8)&255u,(s.role_gw[i]>>16)&255u,(s.role_gw[i]>>24)&255u);
      l->gateway_valid=1;
     }

    if(!route){
        switch(out->default_mask){
        case 0:out->default_egress=Y4_NET_EGRESS_NONE;break;
        case 1:out->default_egress=Y4_NET_EGRESS_LAN;break;
        case 2:out->default_egress=Y4_NET_EGRESS_BOND;break;
        case 4:out->default_egress=Y4_NET_EGRESS_OTHER;break;
        default:out->default_egress=Y4_NET_EGRESS_AMBIGUOUS;break;
        }
    }
    if(close(fd)&&!rc)rc=-errno;
    if(!rc)rc=address?address:route;
    out->error=rc;return rc;
}
