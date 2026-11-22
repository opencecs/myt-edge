/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include "../display/y4_display.h"
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#define RUNTIME "/run/y4-board"
#define UIDDIR "/run/y4-hardware"
static int private_dir(const char *name) {
 if(mkdir(name,0700)&&errno!=EEXIST)return -errno;struct stat s;if(lstat(name,&s))return -errno;
 return S_ISDIR(s.st_mode)&&!s.st_uid&&(s.st_mode&07777u)==0700u?0:-EPERM;
}
static uint32_t crc32c(const unsigned char*p,size_t bytes) {
 uint32_t c=~0u;for(size_t i=0;i<bytes;i++){c^=p[i];for(unsigned b=0;b<8u;b++)c=(c>>1)^((0u-(c&1u))&UINT32_C(0x82f63b78));}return ~c;
}
static void put(unsigned char*p,uint64_t v,unsigned n){for(unsigned i=0;i<n;i++)p[i]=(unsigned char)(v>>(i*8u));}
static int uid_decode(const char *hex,unsigned char uid[12]) {
 if(strlen(hex)!=24u)return -EINVAL;unsigned any=0,notff=0;
 for(unsigned i=0;i<12u;i++){unsigned value=0;
  for(unsigned j=0;j<2u;j++){unsigned char c=(unsigned char)hex[i*2u+j];
   unsigned v=c>='0'&&c<='9'?(unsigned)(c-'0'):c>='a'&&c<='f'?(unsigned)(c-'a')+10u:16u;
   if(v>15u)return -EINVAL;value=(value<<4)|v;}
  uid[i]=(unsigned char)value;any|=value;notff|=value^255u;}
 return any&&notff?0:-ENODATA;
}
static int publish_uid(const char *text) {
 unsigned char uid[12];int e=uid_decode(text,uid);if(e)return e;
 e=private_dir(UIDDIR);if(e)return e;
 int dir=open(UIDDIR,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);if(dir<0)return -errno;
 struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t)||t.tv_sec<0){close(dir);return -EIO;}
 uint64_t now=(uint64_t)t.tv_sec*1000u+(uint64_t)t.tv_nsec/1000000u;if(!now){close(dir);return -EIO;}
 unsigned char raw[64]={0};memcpy(raw,"Y4UIDS01",8);put(raw+8,now,8);memcpy(raw+24,uid,12);put(raw+60,crc32c(raw,60),4);
 char name[80];int n=snprintf(name,sizeof name,".uid-%ld-%llu",(long)getpid(),(unsigned long long)now);
 if(n<0||(size_t)n>=sizeof name){close(dir);return -EOVERFLOW;}
 int fd=openat(dir,name,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);if(fd<0){e=-errno;close(dir);return e;}
 size_t at=0;while(at<sizeof raw){ssize_t wrote=write(fd,raw+at,sizeof raw-at);if(wrote<0&&errno==EINTR)continue;
  if(wrote<=0){e=wrote<0?-errno:-EIO;break;}at+=(size_t)wrote;}
 if(!e&&fsync(fd))e=-errno;if(close(fd)&&!e)e=-errno;
 if(!e&&renameat(dir,name,dir,"uid.bin"))e=-errno;if(!e&&fsync(dir))e=-errno;
 if(e)(void)unlinkat(dir,name,0);close(dir);return e;
}
static int clear_uid(void) {
 int e=private_dir(UIDDIR);if(e)return e;
 int dir=open(UIDDIR,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);if(dir<0)return -errno;
 struct stat s;if(fstatat(dir,"uid.bin",&s,AT_SYMLINK_NOFOLLOW))e=errno==ENOENT?0:-errno;
 else if(!S_ISREG(s.st_mode)||s.st_uid||s.st_nlink!=1u||(s.st_mode&07777u)!=0600u)e=-EPERM;
 else if(unlinkat(dir,"uid.bin",0))e=-errno;
 if(!e&&fsync(dir))e=-errno;close(dir);return e;
}
static int number(const char*text,long low,long high,long*out) {
 if(!text||!*text)return -EINVAL;char*end;errno=0;long v=strtol(text,&end,10);
 if(errno||*end||v<low||v>high)return -EINVAL;*out=v;return 0;
}
static int display(const char *phase,const char *attempt_text,const char *status_text) {
 long attempt,status;int e=number(attempt_text,0,5,&attempt);if(!e)e=number(status_text,-4095,0,&status);if(e)return e;
 uint8_t state;
 if(!strcmp(phase,"checking"))state=VIEW_INIT_CHECKING;
 else if(!strcmp(phase,"mcu"))state=VIEW_INIT_MCU;
 else if(!strcmp(phase,"nic"))state=VIEW_INIT_NIC;
 else if(!strcmp(phase,"ready"))state=VIEW_INIT_READY;
 else if(!strcmp(phase,"failed"))state=VIEW_INIT_FAILED;
 else if(!strcmp(phase,"cancelled"))state=VIEW_INIT_CANCELLED;
 else return -EINVAL;
 e=private_dir(RUNTIME);if(e)return e;
 int lock=open(RUNTIME "/mcu.lock",O_RDWR|O_CREAT|O_NOFOLLOW|O_CLOEXEC,0600);if(lock<0)return -errno;
 struct stat s;if(fstat(lock,&s))e=-errno;
 else if(!S_ISREG(s.st_mode)||s.st_uid||s.st_nlink!=1u||(s.st_mode&07777u)!=0600u)e=-EPERM;
 if(!e&&flock(lock,LOCK_EX|LOCK_NB))e=-errno;
 int fd=-1;if(!e){fd=open("/dev/i2c-1",O_RDWR|O_NOFOLLOW|O_CLOEXEC);if(fd<0)e=-errno;}
 if(!e){struct y4_display_input input={.max_attempts=5,.attempt=(uint8_t)attempt,
  .initialization_valid=1,.initialization_state=state,.initialization_error=(int)status,
  .network_error=-EINPROGRESS,.sample_age_ms=0};
  e=y4_display_publish(fd,&input);}
 if(fd>=0)close(fd);close(lock);return e;
}
int main(int argc,char**argv) {
 if(geteuid()){fputs("Root required\n",stderr);return 1;}
 int e;
 if(argc==3&&!strcmp(argv[1],"--publish-uid"))e=publish_uid(argv[2]);
 else if(argc==2&&!strcmp(argv[1],"--clear-uid"))e=clear_uid();
 else if(argc==5&&!strcmp(argv[1],"--display"))e=display(argv[2],argv[3],argv[4]);
 else{fputs("Usage: y4-board-boot-helper --clear-uid | --publish-uid HEX | --display checking|mcu|nic|ready|failed|cancelled ATTEMPT STATUS\n",stderr);return 2;}
 printf("{\"schema\":1,\"status\":%d}\n",e);return e?1:0;
}
