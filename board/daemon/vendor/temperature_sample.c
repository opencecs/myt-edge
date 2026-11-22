#define _GNU_SOURCE
#include "y4_os_temperature_query.h"
#include "y4_os_cooling.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/magic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/statfs.h>
#include <unistd.h>
static int text(int dir,const char *name,char out[64])
{
 int fd=openat(dir,name,O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK);if(fd<0)return -errno;
 char data[64];ssize_t n;do{n=read(fd,data,sizeof(data));}while(n<0&&errno==EINTR);
 int rc=n<0?-errno:!n||n==64?-EOVERFLOW:0;
 if(close(fd)&&!rc)rc=-errno;
 if(rc)return rc;
 if(memchr(data,0,(size_t)n))return -EBADMSG;
 while(n>0&&(data[n-1]=='\n'||data[n-1]=='\r'||data[n-1]==' '||data[n-1]=='\t'))--n;
 data[n]=0;memcpy(out,data,(size_t)n+1u);return n?0:-EBADMSG;
}
static void reading(int dir,unsigned index,struct y4_temperature_reading *out)
{
 *out=(struct y4_temperature_reading){.error=-ENOENT};char name[32],data[64];
 snprintf(name,sizeof(name),"temp%u_input",index);int rc=text(dir,name,data);
 if(!rc){char *end;errno=0;long long v=strtoll(data,&end,10);
  if(errno||end==data||*end)rc=-EBADMSG;
  else if(v< -40000||v>150000)rc=-ERANGE;
  else{out->readable=1;out->millicelsius=(int64_t)v;}}
 out->error=rc;
}
int y4_temperature_read(const struct y4_call *call,struct y4_temperature_snapshot *out)
{
 if(!call||!call->opaque||!out)return -EINVAL;
 const struct y4_temperature_context *context=call->opaque;
 if(!context->sysroot||context->sysroot[0]!='/'||(context->require_sysfs!=0&&context->require_sysfs!=1))return -EINVAL;
 struct y4_temperature_snapshot result={.soc_error=-ENODATA,.amb={.error=-ENOENT},.user={.error=-ENOENT}};
 char path[512];int n=snprintf(path,sizeof(path),"%s/class/hwmon",context->sysroot);
 if(n<0||(size_t)n>=sizeof(path))return -ENAMETOOLONG;
 DIR *list=opendir(path);if(!list){result.scan_error=-errno;result.soc_error=-errno;result.amb.error=result.user.error=-errno;*out=result;return 0;}
 int rc=0;unsigned amb_seen=0,user_seen=0,soc_failures=0;
 if(context->require_sysfs){struct statfs fs;if(fstatfs(dirfd(list),&fs))rc=-errno;else if(fs.f_type!=SYSFS_MAGIC)rc=-EXDEV;}
 struct dirent *entry;unsigned seen=0;
 while(!rc&&(entry=readdir(list))){
  rc=y4_command_poll(call);if(rc)break;
  if(strncmp(entry->d_name,"hwmon",5))continue;
  const char *p=entry->d_name+5;if(!*p)continue;int numeric=1;for(;*p;++p)if(*p<'0'||*p>'9')numeric=0;
  if(!numeric)continue;
  /* Kernel class entries are intentionally symlinks; retain their directory. */
  int device=openat(dirfd(list),entry->d_name,O_RDONLY|O_DIRECTORY|O_CLOEXEC);if(device<0){if(!result.scan_error)result.scan_error=-errno;continue;}
  char name[64];int found=text(device,"name",name);
  if(found&&!result.scan_error)result.scan_error=found;
  if(!found&&!strcmp(name,"scmi_sensors")){
   if(++seen>1u){rc=-EEXIST;close(device);break;}
   result.scmi_present=1;
   for(unsigned index=1;index<=64u;++index){
    rc=y4_command_poll(call);if(rc)break;
    char file[32],label[64];snprintf(file,sizeof(file),"temp%u_label",index);
    int label_error=text(device,file,label);
    if(label_error){if(label_error!=-ENOENT&&!result.scan_error)result.scan_error=label_error;continue;}
    int amb=!strcmp(label,"PCB_AMB"),user=!strcmp(label,"PCB_USER");
    int soc=!strncasecmp(label,"CPU",3)||!strncasecmp(label,"SOC",3)||
      !strncasecmp(label,"GPU",3)||!strncasecmp(label,"CORE",4)||!strncasecmp(label,"DSU",3);
    if(!amb&&!user&&!soc){++result.unknown_count;continue;}
    struct y4_temperature_reading value;reading(device,index,&value);
    if(amb){if(amb_seen++){rc=-EEXIST;break;}result.amb=value;}
    if(user){if(user_seen++){rc=-EEXIST;break;}result.user=value;}
    if(soc){if(value.readable){if(!result.soc_valid||value.millicelsius>result.soc_max)result.soc_max=value.millicelsius;
      result.soc_valid=1;++result.soc_count;if(!soc_failures)result.soc_error=0;}
     else{if(!soc_failures)result.soc_error=value.error;++soc_failures;}}
   }
  }
  if(close(device)&&!rc)rc=-errno;
 }
 if(closedir(list)&&!rc)rc=-errno;
 if(rc)return rc;
 if(context->require_sysfs && !strcmp(context->sysroot,"/sys")){
  result.pm_checked=1;result.pm_error=y4_tmp75_pm_read(call,&result.pm);
 }
 *out=result;return 0;
}
