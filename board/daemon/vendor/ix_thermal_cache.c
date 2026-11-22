#define _GNU_SOURCE
#include "y4_os_ix_thermal.h"
#include "y4_os_ix_topology.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#define WIRE 256u
#define NAME "thermal.bin"
static void p32(unsigned char *p,uint32_t v){for(unsigned i=0;i<4;i++)p[i]=(unsigned char)(v>>(8*i));}
static uint32_t g32(const unsigned char *p){uint32_t v=0;for(unsigned i=0;i<4;i++)v|=(uint32_t)p[i]<<(8*i);return v;}
static void p64(unsigned char *p,uint64_t v){for(unsigned i=0;i<8;i++)p[i]=(unsigned char)(v>>(8*i));}
static uint64_t g64(const unsigned char *p){uint64_t v=0;for(unsigned i=0;i<8;i++)v|=(uint64_t)p[i]<<(8*i);return v;}
static uint32_t crc(const unsigned char *p,size_t n){uint32_t c=~0u;for(size_t i=0;i<n;i++){c^=p[i];for(unsigned b=0;b<8;b++)c=(c>>1)^((0u-(c&1u))&0x82f63b78u);}return ~c;}
static int err_ok(int v){return v<=0&&v>=-4095;}
static int fail(struct y4_ix_thermal_sample *o,int e){memset(o,0,sizeof *o);o->status=e;o->temperature_error=e;return e;}
static int check_dir(int d){struct stat st;if(fstat(d,&st))return -errno;return S_ISDIR(st.st_mode)&&st.st_uid==geteuid()&&(st.st_mode&07777)==0700?0:-EACCES;}
static int valid_sample(const struct y4_ix_thermal_sample *s){
 unsigned nz=0;for(unsigned i=0;i<16;i++)nz|=s->boot_id[i];
 if(!nz||!s->generation||!s->sampled_ns||s->completed_ns<s->sampled_ns||
    !err_ok(s->status)||!err_ok(s->temperature_error)||s->valid<0||s->valid>1||
    !err_ok(s->result.status)||s->result.unreaped_pid<0||
    s->result.stage>5||s->result.reads_completed>40||s->result.mapping_verified>1)return -EPROTO;
 if(s->bdf[0]){if(!memchr(s->bdf,0,13)||!y4_ix_bdf_valid(s->bdf))return -EPROTO;}
 for(unsigned i=(unsigned)strnlen(s->bdf,13)+1;i<13;i++)if(s->bdf[i])return -EPROTO;
 if(s->valid){
  if(s->status||s->temperature_error||s->millicelsius< -40000||s->millicelsius>125000||
     !s->result.mapping_verified||s->result.status||s->result.stage!=5||
     s->result.control0!=0x11807u||s->result.control1!=0x1fu||!s->bdf[0])return -EPROTO;
 }else if(s->millicelsius||!s->temperature_error)return -EPROTO;
 if(s->status&&s->valid)return -EPROTO;
 return 0;
}
static void bar_put(unsigned char *p,const ix_bar_probe_result *r){
 p32(p,(uint32_t)r->status);p32(p+4,r->stage);p32(p+8,r->failed_offset);p32(p+12,r->reads_completed);p32(p+16,r->mapping_verified);
 const uint32_t *a[5]={r->ids,r->classes,r->pcie_headers,r->link_dwords,r->training_status};
 for(unsigned x=0;x<5;x++)for(unsigned y=0;y<5;y++)p32(p+20+20*x+4*y,a[x][y]);
 p32(p+120,r->raw_temperature);p32(p+124,r->calibration);p32(p+128,r->management_mode);
 p32(p+132,r->control0);p32(p+136,r->control1);p32(p+140,(uint32_t)r->unreaped_pid);
}
static void bar_get(const unsigned char *p,ix_bar_probe_result *r){
 r->status=(int32_t)g32(p);r->stage=g32(p+4);r->failed_offset=g32(p+8);r->reads_completed=g32(p+12);r->mapping_verified=g32(p+16);
 uint32_t *a[5]={r->ids,r->classes,r->pcie_headers,r->link_dwords,r->training_status};
 for(unsigned x=0;x<5;x++)for(unsigned y=0;y<5;y++)a[x][y]=g32(p+20+20*x+4*y);
 r->raw_temperature=g32(p+120);r->calibration=g32(p+124);r->management_mode=g32(p+128);
 r->control0=g32(p+132);r->control1=g32(p+136);r->unreaped_pid=(pid_t)g32(p+140);
}
int y4_ix_thermal_invalidate_at(int d){int e=check_dir(d);if(e)return e;return unlinkat(d,NAME,0)&&errno!=ENOENT?-errno:0;}
int y4_ix_thermal_store_at(int d,const struct y4_ix_thermal_sample *s){
 int e=check_dir(d);if(e)return e;if(!s)return -EINVAL;
 e=valid_sample(s);if(e){(void)y4_ix_thermal_invalidate_at(d);return e;}
 unsigned char w[WIRE]={0};memcpy(w,"Y4IXT001",8);p64(w+8,s->sampled_ns);p64(w+16,s->completed_ns);p64(w+24,s->generation);
 p32(w+32,(uint32_t)s->status);p32(w+36,(uint32_t)s->temperature_error);p32(w+40,(uint32_t)s->valid);p32(w+44,(uint32_t)s->millicelsius);
 memcpy(w+48,s->boot_id,16);memcpy(w+64,s->bdf,13);bar_put(w+80,&s->result);p32(w+252,crc(w,252));
 char name[80];int n=snprintf(name,sizeof name,".thermal-%ld-%llu",(long)getpid(),(unsigned long long)s->generation);
 if(n<0||(size_t)n>=sizeof name){(void)y4_ix_thermal_invalidate_at(d);return -EOVERFLOW;}
 int fd=openat(d,name,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
 if(fd<0){e=-errno;goto failed;}
 if(fchmod(fd,0600))e=-errno;
 size_t done=0;unsigned interruptions=0;
 while(!e&&done<WIRE){ssize_t k=write(fd,w+done,WIRE-done);if(k<0){if(errno==EINTR&&++interruptions<8)continue;e=-errno;}else if(!k)e=-EIO;else done+=(size_t)k;}
 if(close(fd)&&!e)e=-errno;
 if(!e&&renameat(d,name,d,NAME))e=-errno;
 if(e)(void)unlinkat(d,name,0);
failed:
 if(e)(void)y4_ix_thermal_invalidate_at(d);
 return e;
}
int y4_ix_thermal_load_at(int d,const unsigned char boot[16],uint64_t now,struct y4_ix_thermal_sample *o){
 if(!o)return -EINVAL;
 fail(o,-EINVAL);if(!boot||!now)return -EINVAL;
 int e=check_dir(d);if(e)return fail(o,e);
 int fd=openat(d,NAME,O_RDONLY|O_NONBLOCK|O_NOFOLLOW|O_CLOEXEC);if(fd<0)return fail(o,-errno);
 struct stat st;unsigned char w[WIRE+1];size_t done=0;unsigned interruptions=0;
 if(fstat(fd,&st))e=-errno;
 else if(!S_ISREG(st.st_mode)||st.st_uid!=geteuid()||(st.st_mode&07777)!=0600||st.st_nlink!=1)e=-EACCES;
 else if(st.st_size!=WIRE)e=-EPROTO;
 while(!e&&done<sizeof w){ssize_t k=read(fd,w+done,sizeof w-done);if(k<0){if(errno==EINTR&&++interruptions<8)continue;e=-errno;}else if(!k)break;else done+=(size_t)k;}
 if(close(fd)&&!e)e=-errno;
 if(e)return fail(o,e);
 if(done!=WIRE||memcmp(w,"Y4IXT001",8)||crc(w,252)!=g32(w+252))return fail(o,-EBADMSG);
 for(unsigned i=77;i<80;i++)if(w[i])return fail(o,-EPROTO);
 for(unsigned i=224;i<252;i++)if(w[i])return fail(o,-EPROTO);
 struct y4_ix_thermal_sample s={0};s.sampled_ns=g64(w+8);s.completed_ns=g64(w+16);s.generation=g64(w+24);
 s.status=(int32_t)g32(w+32);s.temperature_error=(int32_t)g32(w+36);s.valid=(int)g32(w+40);s.millicelsius=(int32_t)g32(w+44);
 memcpy(s.boot_id,w+48,16);memcpy(s.bdf,w+64,13);bar_get(w+80,&s.result);
 e=valid_sample(&s);if(e)return fail(o,e);
 if(memcmp(boot,s.boot_id,16)||now<s.completed_ns||now-s.sampled_ns>=Y4_IX_THERMAL_MAX_AGE_NS)return fail(o,-ESTALE);
 s.age_ms=(now-s.sampled_ns)/1000000u;*o=s;return 0;
}
