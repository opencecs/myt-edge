#define _GNU_SOURCE
#include "y4_os_mcu_uid_snapshot.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <sys/stat.h>
#include <unistd.h>
static int directory(int fd){struct stat st;if(fstat(fd,&st))return -errno;return S_ISDIR(st.st_mode)&&!st.st_uid&&(st.st_mode&07777u)==0700u?0:-EACCES;}
int y4_mcu_uid_store(int dir,const struct y4_mcu_uid_snapshot *s){if(geteuid())return -EPERM;unsigned char raw[64];int rc=y4_mcu_uid_snapshot_encode(s,raw,sizeof(raw));if(rc)return rc;rc=directory(dir);if(rc)return rc;static uint64_t sequence;char name[64];int fd=-1;for(unsigned attempt=0;attempt<16;++attempt){if(sequence==UINT64_MAX)return -EOVERFLOW;++sequence;snprintf(name,sizeof(name),".uid-%ld-%llu",(long)getpid(),(unsigned long long)sequence);fd=openat(dir,name,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);if(fd>=0)break;if(errno!=EEXIST)return -errno;}if(fd<0)return -EEXIST;if(fchmod(fd,0600))rc=-errno;size_t at=0;while(!rc&&at<sizeof(raw)){ssize_t n=write(fd,raw+at,sizeof(raw)-at);if(n<0&&errno==EINTR)continue;if(n<=0){rc=n<0?-errno:-EIO;break;}at+=(size_t)n;}if(close(fd)&&!rc)rc=-errno;if(!rc&&renameat(dir,name,dir,"uid.bin"))rc=-errno;if(rc)(void)unlinkat(dir,name,0);return rc;}
int y4_mcu_uid_load(int dir,uint64_t now,struct y4_mcu_uid_snapshot *out){if(!out)return -EINVAL;int rc=directory(dir);if(rc)return rc;int fd=openat(dir,"uid.bin",O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC);if(fd<0)return -errno;struct stat st;if(fstat(fd,&st))rc=-errno;else if(!S_ISREG(st.st_mode)||st.st_uid||(st.st_mode&07777u)!=0600u||st.st_nlink>1u)rc=-EACCES;else if(st.st_size!=64)rc=-EPROTO;unsigned char raw[65];size_t at=0;while(!rc&&at<sizeof(raw)){ssize_t n=read(fd,raw+at,sizeof(raw)-at);if(n<0&&errno==EINTR)continue;if(n<0){rc=-errno;break;}if(!n)break;at+=(size_t)n;}if(close(fd)&&!rc)rc=-errno;return rc?rc:y4_mcu_uid_snapshot_decode(raw,at,now,out);}

int y4_mcu_uid_invalidate(int dir)
{
 if(geteuid())return -EPERM;
 int rc=directory(dir);if(rc)return rc;
 return unlinkat(dir,"uid.bin",0)&&errno!=ENOENT?-errno:0;
}
