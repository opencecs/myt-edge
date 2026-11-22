#define _GNU_SOURCE
#include "y4_os_network_permanent.h"
#include <errno.h>
#include <limits.h>
#include <linux/ethtool.h>
#include <linux/sockios.h>
#include <net/if.h>
#include <net/if_arp.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>
static int index_matches(int fd,struct ifreq *r,unsigned index)
{
 if(if_nametoindex(r->ifr_name)!=index)return -ESTALE;
 if(ioctl(fd,SIOCGIFINDEX,r))return -errno;
 return r->ifr_ifindex>0&&(unsigned)r->ifr_ifindex==index?0:-ESTALE;
}
int y4_network_permanent_mac(const char *name,unsigned index,unsigned char out[6])
{
 if(!name||!out||!*name||strnlen(name,IFNAMSIZ)>=IFNAMSIZ||!index||index>INT_MAX)return -EINVAL;
 int fd=socket(AF_INET,SOCK_DGRAM|SOCK_CLOEXEC,0);if(fd<0)return -errno;
 struct ifreq r={0};memcpy(r.ifr_name,name,strlen(name)+1u);
 int rc=index_matches(fd,&r,index);unsigned char observed[6];
 struct ethtool_perm_addr *p=NULL;
 if(!rc&&ioctl(fd,SIOCGIFHWADDR,&r))rc=-errno;
 if(!rc&&r.ifr_hwaddr.sa_family!=ARPHRD_ETHER)rc=-ENODEV;
 if(!rc){
  p=calloc(1,sizeof(*p)+32u);if(!p)rc=-ENOMEM;
 }
 if(!rc){
  p->cmd=ETHTOOL_GPERMADDR;p->size=32u;r.ifr_data=(char *)p;
  if(ioctl(fd,SIOCETHTOOL,&r))rc=-errno;
  else if(p->cmd!=ETHTOOL_GPERMADDR||p->size!=6u)rc=-EPROTO;
  else{
   unsigned any=0;for(unsigned i=0;i<6u;++i)any|=p->data[i];
   if(!any||(p->data[0]&1u))rc=-ENODATA;
   else memcpy(observed,p->data,6);
  }
 }
 if(!rc)rc=index_matches(fd,&r,index);
 free(p);if(close(fd)&&!rc)rc=-errno;
 if(!rc)memcpy(out,observed,6);
 return rc;
}
