#define _GNU_SOURCE
#include "y4_os_ix_bar_query.h"
#include "y4_os_ix_thermal.h"
#include "y4_os_ix_probe.h"
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>
static int checked(const struct y4_call *call){return y4_command_poll(call);}
static int child_poll(void *p){return checked(p);}
int y4_ix_bar_collect_snapshot(const struct y4_call *call,const struct y4_ix_snapshot *snap,ix_bar_probe_result *result,char bdf[13])
{
 if(!call||!snap||!result||!bdf)return -EINVAL;
 memset(result,0,sizeof *result);memset(bdf,0,13);
 int rc=checked(call);if(rc)return rc;
 const struct y4_ix_inventory *v=&snap->inventory;
 if(v->topology.stage!=Y4_IX_TOPOLOGY_READY||v->topology.upstream_index<0)return -EAGAIN;
 int root=-1,dirs[5]={-1,-1,-1,-1,-1},fds[5]={-1,-1,-1,-1,-1},bar=-1,lock=-1;
 {
  lock=open("/run/y4-ix-bar.lock",O_RDWR|O_CREAT|O_NOFOLLOW|O_CLOEXEC,0600);
  if(lock<0)rc=-errno;
  else {struct stat s;if(fstat(lock,&s))rc=-errno;
   else if(!S_ISREG(s.st_mode)||s.st_uid||s.st_nlink!=1||(s.st_mode&07777u)!=0600u)rc=-EPERM;
   else if(flock(lock,LOCK_EX|LOCK_NB))rc=errno==EWOULDBLOCK?-EBUSY:-errno;}
 }
 if(rc){if(lock>=0)close(lock);return rc;}
 root=open("/sys/bus/pci/devices",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);if(root<0){rc=-errno;close(lock);return rc;}
 int32_t indices[5]={v->topology.upstream_index,-1,-1,-1,-1};
 unsigned portnos[4]={0,3,6,9};
 for(unsigned step=0;step<5&&!rc;step++){
  int32_t index=step?v->topology.downstream_index[step-1]:indices[0];
  if(index<0||(uint32_t)index>=v->count){rc=-ENODEV;break;}
  const struct y4_ix_node *node=&v->nodes[(uint32_t)index];
  if(node->identity_error||node->vendor!=0x1ec8||node->device!=0x8024||!y4_ix_bdf_valid(node->bdf)){rc=-ENODEV;break;}
  int dir=openat(root,node->bdf,O_RDONLY|O_DIRECTORY|O_CLOEXEC);if(dir<0){rc=-errno;break;}
  struct y4_ix_probe probe;rc=y4_ix_probe_read_fd(dir,call,&probe);if(rc){close(dir);break;}
  if(probe.pcie_error||!probe.pcie_present){rc=-ENODATA;close(dir);break;}
  unsigned position=0;
  if(step){
   if(probe.pcie_type!=6){rc=-ENODEV;close(dir);break;}
   unsigned port=probe.pcie_link_cap>>24;position=5;
   for(unsigned j=0;j<4;j++)if(port==portnos[j])position=j+1;
   if(position>=5||dirs[position]>=0){rc=-EUCLEAN;close(dir);break;}
  }else{
   if(probe.pcie_type!=5||probe.resource_error||probe.resources[0].length!=IX_BAR_PROBE_BYTES||!(probe.resources[0].flags&0x200u)){rc=-ENODEV;close(dir);break;}
   memcpy(bdf,node->bdf,13);
  }
  dirs[position]=dir;
  fds[position]=openat(dir,"config",O_RDONLY|O_NOFOLLOW|O_CLOEXEC);if(fds[position]<0){rc=-errno;break;}
 }
 if(!rc){
  bar=openat(dirs[0],"resource0",O_RDONLY|O_NOFOLLOW|O_CLOEXEC);if(bar<0)rc=-errno;
 }
 if(!rc)rc=ix_bar_probe_run(bar,fds,child_poll,(void *)call,result);
 /* The isolated child inherits the lease: if it remains uninterruptible,
  * closing our fd cannot unlock it prematurely for another probe. */
 if(lock>=0)close(lock);
 if(bar>=0)close(bar);
 for(unsigned i=0;i<5;i++){if(fds[i]>=0)close(fds[i]);if(dirs[i]>=0)close(dirs[i]);}
 close(root);return rc;
}
