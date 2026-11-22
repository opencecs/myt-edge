/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include "board_daemon.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
volatile sig_atomic_t y4_board_stopping;
uint64_t y4_board_now_ms(void) {
 struct timespec t;if(clock_gettime(CLOCK_BOOTTIME,&t)||t.tv_sec<0)return 0;
 return (uint64_t)t.tv_sec*1000u+(uint64_t)t.tv_nsec/1000000u;
}
int y4_board_private_dir(const char *path) {
 if(mkdir(path,0700)&&errno!=EEXIST)return -errno;
 struct stat s;if(lstat(path,&s))return -errno;
 return S_ISDIR(s.st_mode)&&s.st_uid==0&&(s.st_mode&07777u)==0700u?0:-EPERM;
}
int y4_board_read_file(const char *path,char *out,size_t cap,size_t *bytes) {
 if(!path||!out||cap<2u)return -EINVAL;
 int fd=open(path,O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC);if(fd<0)return -errno;
 struct stat s;int rc=0;
 if(fstat(fd,&s))rc=-errno;
 else if(!S_ISREG(s.st_mode)||s.st_uid||s.st_nlink!=1u||(s.st_mode&0022u)||s.st_size<0||(uint64_t)s.st_size>=cap)rc=-EPERM;
 size_t at=0;
 while(!rc&&at<cap-1u){ssize_t n=read(fd,out+at,cap-1u-at);
  if(n<0){if(errno==EINTR)continue;rc=-errno;}else if(!n)break;else at+=(size_t)n;}
 if(close(fd)&&!rc)rc=-errno;
 if(!rc){out[at]=0;if(memchr(out,0,at))rc=-EBADMSG;}
 if(!rc&&bytes)*bytes=at;return rc;
}
int y4_board_atomic_file(const char *directory,const char *name,const void *raw,size_t bytes,mode_t mode) {
 if(!directory||!name||strchr(name,'/')||!raw||!bytes)return -EINVAL;
 int dir=open(directory,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);if(dir<0)return -errno;
 struct stat st;int rc=fstat(dir,&st)?-errno:0;
 if(!rc&&(!S_ISDIR(st.st_mode)||st.st_uid||(st.st_mode&07777u)!=0700u))rc=-EPERM;
 static unsigned sequence;char temporary[96];
 if(!rc){int n=snprintf(temporary,sizeof temporary,".%s-%ld-%u",name,(long)getpid(),++sequence);
  if(n<0||(size_t)n>=sizeof temporary)rc=-ENAMETOOLONG;}
 int fd=-1;if(!rc){fd=openat(dir,temporary,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,mode);if(fd<0)rc=-errno;}
 size_t at=0;while(!rc&&at<bytes){ssize_t n=write(fd,(const char*)raw+at,bytes-at);
  if(n<0){if(errno==EINTR)continue;rc=-errno;}else if(!n)rc=-EIO;else at+=(size_t)n;}
 if(!rc&&fchmod(fd,mode))rc=-errno;
 if(!rc&&fsync(fd))rc=-errno;
 if(fd>=0&&close(fd)&&!rc)rc=-errno;
 if(!rc&&renameat(dir,temporary,dir,name))rc=-errno;
 if(!rc&&fsync(dir))rc=-errno;
 if(rc&&fd>=0)(void)unlinkat(dir,temporary,0);
 if(close(dir)&&!rc)rc=-errno;return rc;
}
static int blank(char c){return c==' '||c=='\t'||c=='\r'||c=='\n';}
/* Locate a key of the root JSON object with a bounded scan. The scanner
 * tracks nesting depth and only treats a string at depth 1 that is followed
 * by ':' as a key, so embedded status receipts (mcu_status / nic_status
 * objects published by the initializer) can neither shadow nor collide with
 * top-level fields. A duplicated key inside the root object remains an
 * error; nested objects keep their own key spaces. */
static int field(const char *text,const char *key,const char **value) {
 if(!key||!*key||strlen(key)>64u)return -EINVAL;
 const char *p=text,*match=NULL;size_t klen=strlen(key);
 int depth=0;char kinds[32];
 while(blank(*p))p++;
 if(*p++!='{')return -EBADMSG;
 kinds[depth++]='{';
 while(*p){
  if(blank(*p)||*p==','){p++;continue;}
  if(*p=='{'||*p=='['){
   if(depth>=32)return -EBADMSG;
   kinds[depth++]=*p;
   p++;continue;
  }
  if(*p=='}'||*p==']'){
   if(!depth)return -EBADMSG;
   depth--;
   if((*p=='}'&&kinds[depth]!='{')||(*p==']'&&kinds[depth]!='['))return -EBADMSG;
   p++;
   if(!depth){while(blank(*p))p++;return *p?-EBADMSG:(match?(*value=match,0):-ENODATA);}
   continue;
  }
  /* Numbers, true/false/null and any other raw scalar content are consumed
   * one character at a time; the typed accessors validate the values they
   * actually read, so the scanner only needs correct traversal. */
  if(*p!='"'){p++;continue;}
  const char *start=++p;
  while(*p&&*p!='"'){if(*p=='\\'&&p[1])p+=2;else p++;}
  if(!*p)return -EBADMSG;
  size_t len=(size_t)(p-start);
  const char *after=p+1;
  while(blank(*after))after++;
  if(*after==':'){ /* key position */
   if(depth==1&&len==klen&&!memcmp(start,key,klen)){
    if(match)return -EBADMSG; /* duplicate root key */
    const char *v=after+1;
    while(blank(*v))v++;
    match=v;
   }
   p=after+1;
   while(blank(*p))p++;
   continue;
  }
  p=after; /* string value: skip it */
 }
 return -EBADMSG;
}
static int ending(const char *p) {
 while(*p==' '||*p=='\t'||*p=='\r'||*p=='\n')p++;
 return *p==','||*p=='}'||!*p;
}
int y4_board_json_number(const char *text,const char *key,long long *out) {
 if(!text||!key||!out)return -EINVAL;const char *p;int rc=field(text,key,&p);if(rc)return rc;
 if(!((*p>='0'&&*p<='9')||(*p=='-'&&p[1]>='0'&&p[1]<='9')))return -EBADMSG;
 char *e;errno=0;long long v=strtoll(p,&e,10);if(errno||!ending(e))return -EBADMSG;
 if((*p=='0'&&e-p>1)||(*p=='-'&&p[1]=='0'&&e-p>2))return -EBADMSG;*out=v;return 0;
}
int y4_board_json_unsigned(const char *text,const char *key,uint64_t *out) {
 if(!text||!key||!out)return -EINVAL;const char*p;int rc=field(text,key,&p);if(rc)return rc;
 if(*p<'0'||*p>'9')return -EBADMSG;uint64_t value=0;const char*at=p;
 while(*at>='0'&&*at<='9'){unsigned digit=(unsigned)(*at++-'0');
  if(value>(UINT64_MAX-digit)/10u)return -EOVERFLOW;value=value*10u+digit;}
 if(!ending(at)||(*p=='0'&&at-p>1))return -EBADMSG;*out=value;return 0;
}
int y4_board_json_bool(const char *text,const char *key,int *out) {
 const char *p;int rc=field(text,key,&p);if(rc)return rc;
 if(!strncmp(p,"true",4)&&ending(p+4)){*out=1;return 0;}
 if(!strncmp(p,"false",5)&&ending(p+5)){*out=0;return 0;}
 return -EBADMSG;
}
int y4_board_json_string(const char *text,const char *key,char *out,size_t cap) {
 const char *p;int rc=field(text,key,&p);if(rc)return rc;if(*p++!='"')return -EBADMSG;
 size_t n=0;while(*p&&*p!='"'){unsigned char c=(unsigned char)*p++;
  if(c<32u||c==92u||n+1u>=cap)return -EBADMSG;out[n++]=(char)c;}
 if(*p!='"'||!ending(p+1))return -EBADMSG;out[n]=0;return 0;
}
int y4_board_initialization_read(struct y4_board_initialization *out) {
 if(!out)return -EINVAL;memset(out,0,sizeof *out);out->max_attempts=5;
 char text[8192];int rc=y4_board_read_file(Y4_BOARD_INIT_STATUS,text,sizeof text,NULL);if(rc)return rc;
 long long schema,attempt,maximum,status;
 if((rc=y4_board_json_number(text,"schema",&schema))||schema!=1)return rc?rc:-EPROTO;
 if((rc=y4_board_json_number(text,"attempt",&attempt))||attempt<0||attempt>5)return rc?rc:-EPROTO;
 if((rc=y4_board_json_number(text,"max_attempts",&maximum))||maximum!=5)return rc?rc:-EPROTO;
 if((rc=y4_board_json_number(text,"status",&status))||status>0||status< -4095)return rc?rc:-EPROTO;
 if((rc=y4_board_json_bool(text,"completed",&out->completed))||
    (rc=y4_board_json_bool(text,"network_safe",&out->network_safe))||
    (rc=y4_board_json_string(text,"phase",out->phase,sizeof out->phase)))return rc;
 out->attempt=(int)attempt;out->max_attempts=(int)maximum;out->status=(int)status;out->valid=1;return 0;
}
