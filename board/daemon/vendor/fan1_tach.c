/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include "y4_os_fan1_tach.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static int text(int dir,const char *name,char out[64]) {
 int fd=openat(dir,name,O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK);
 if(fd<0)return -errno;
 char data[64];ssize_t n;
 do{n=read(fd,data,sizeof(data));}while(n<0&&errno==EINTR);
 int rc=n<0?-errno:!n||n==64?-EOVERFLOW:0;
 if(close(fd)&&!rc)rc=-errno;
 if(rc)return rc;
 if(memchr(data,0,(size_t)n))return -EBADMSG;
 while(n>0&&(data[n-1]=='\n'||data[n-1]=='\r'||data[n-1]==' '||data[n-1]=='\t'))--n;
 data[n]=0;memcpy(out,data,(size_t)n+1u);
 return n?0:-EBADMSG;
}
int y4_soc_fan_tach_read(unsigned fan,uint8_t *valid,uint16_t *rpm) {
 if(fan<1||fan>2||!valid||!rpm)return -EINVAL;
 *valid=0;*rpm=0;
 DIR *list=opendir("/sys/class/hwmon");
 if(!list)return -errno;
 int rc=-ENOENT;
 struct dirent *entry;
 while((entry=readdir(list))) {
  if(strncmp(entry->d_name,"hwmon",5))continue;
  const char *p=entry->d_name+5;
  if(!*p)continue;
  int numeric=1;for(;*p;++p)if(*p<'0'||*p>'9')numeric=0;
  if(!numeric)continue;
  /* Kernel class entries are intentionally symlinks; keep the directory. */
  int device=openat(dirfd(list),entry->d_name,O_RDONLY|O_DIRECTORY|O_CLOEXEC);
  if(device<0){if(rc==-ENOENT)rc=-errno;continue;}
  char name[64];
  int e=text(device,"name",name);
  if(e){close(device);if(rc==-ENOENT)rc=e;continue;}
  if(fan==2 ? strcmp(name,"y4_fan2") : (strcmp(name,"pwmfan")&&strcmp(name,"pwm-fan"))){close(device);continue;}
  char data[64];
  e=text(device,"fan1_input",data);
  close(device);
  if(e){rc=e;break;}
  errno=0;char *end=NULL;
  unsigned long long v=strtoull(data,&end,10);
  if(errno||end==data||*end){rc=-EBADMSG;break;}
  if(v>65535ull){rc=-ERANGE;break;}
  *valid=1;*rpm=(uint16_t)v;rc=0;
  break;
 }
 closedir(list);
 return rc;
}
