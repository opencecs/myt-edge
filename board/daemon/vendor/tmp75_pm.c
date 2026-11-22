#define _GNU_SOURCE
#include "y4_os_tmp75_pm.h"
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include <sys/file.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <time.h>
#include <unistd.h>
#define WINDOW 0xd000u
#define CTRL 0x680u
#define RX_W 0x6a4u
#define RX_R 0x6a8u
#define RX_DATA 0x800u
#define RX_SIZE 0xc800u
#define TX_W 0x6ecu
#define TX_R 0x6f0u
#define TX_DATA 0x700u
#define TX_SIZE 256u
static uint32_t load(volatile unsigned char *p,unsigned off)
{ uint32_t v=*(volatile uint32_t *)(p+off);atomic_thread_fence(memory_order_seq_cst);return v; }
static void store(volatile unsigned char *p,unsigned off,uint32_t v)
{ atomic_thread_fence(memory_order_seq_cst);*(volatile uint32_t *)(p+off)=v;atomic_thread_fence(memory_order_seq_cst); }
static int layout(volatile unsigned char *p)
{
 const char magic[]="CIXPM RTT";
 for(unsigned i=0;i<sizeof(magic);i++)if(p[CTRL+i]!=(unsigned char)magic[i])return -EPROTO;
 if(load(p,CTRL+16u)!=3u||load(p,CTRL+20u)!=1u||load(p,0x6a0u)!=RX_SIZE||load(p,0x6e8u)!=TX_SIZE)return -EPROTO;
 return 0;
}
static int poll_delay(const struct y4_call *call)
{ int rc=y4_command_poll(call);if(rc)return rc;struct timespec t={0,1000000};return nanosleep(&t,NULL)?-errno:0; }
static int receive(volatile unsigned char *p,char *out,size_t cap,size_t *n)
{
 int rc=layout(p);if(rc)return rc;
 uint32_t w=load(p,RX_W),r=load(p,RX_R);if(w>=RX_SIZE||r>=RX_SIZE)return -EPROTO;
 size_t count=w>=r?w-r:RX_SIZE-r+w;
 if(count>cap)count=cap;
 for(size_t i=0;i<count;i++)out[i]=(char)p[RX_DATA+(r+(uint32_t)i)%RX_SIZE];
 store(p,RX_R,(r+(uint32_t)count)%RX_SIZE);*n=count;return 0;
}
static int terminal(const char *text,uint32_t *seq,int *good)
{
 unsigned found=0;const char *p=text;
 while(*p){const char *e=strchr(p,'\n');
  /* The RTT producer may publish a footer in multiple chunks. A status
   * token is authoritative only after its terminating line delimiter. */
  if(!e)break;
  size_t n=(size_t)(e-p);
  if(n>0u && n<128u){char line[128];memcpy(line,p,n);line[n]=0;uint64_t v;char status[16],tail;
   if(sscanf(line,"%" SCNu64 " cli_cmd - %15s %c",&v,status,&tail)==2){
    if(v>UINT32_MAX || ++found!=1u)return -EPROTO;
    *seq=(uint32_t)v;*good=!strcmp(status,"SUCCESS");
   }
  }if(!e)break;p=e+1;
 }return found?0:-EAGAIN;
}
int y4_tmp75_pm_parse(const char *frame,uint32_t expected,unsigned address,uint8_t *out,unsigned count)
{
 if(!frame||!out||!count||count>2u||(address!=0x48u&&address!=0x49u))return -EINVAL;
 uint32_t seq=0;int good=0;int rc=terminal(frame,&seq,&good);if(rc)return rc;
 if(seq!=expected)return -ESTALE;
 if(!good)return -EREMOTEIO;
 char identity[64];int n=snprintf(identity,sizeof(identity),"I2C Ctrl#3, SlvAddr %02Xh",address);
 if(n<0||(size_t)n>=sizeof(identity))return -EINVAL;
 if(!strstr(frame,identity))return -EPROTO;
 if(strstr(frame,">> return fail"))return -EREMOTEIO;
 const char *success=strstr(frame,">> return success"),*data=strstr(frame,">> Data:");
 if(!success||!data||success>data)return -EPROTO;
 data+=strlen(">> Data:");uint8_t bytes[2]={0};
 for(unsigned i=0;i<count;i++){
  while(*data==' '||*data=='\t'||*data=='\n'||*data=='\r')data++;
  if(!((data[0]>='0'&&data[0]<='9')||(data[0]>='A'&&data[0]<='F')||(data[0]>='a'&&data[0]<='f')) ||
     !((data[1]>='0'&&data[1]<='9')||(data[1]>='A'&&data[1]<='F')||(data[1]>='a'&&data[1]<='f')) ||data[2]!='h')return -EBADMSG;
  char hex[3]={data[0],data[1],0};bytes[i]=(uint8_t)strtoul(hex,NULL,16);data+=3;
 }
 while(*data==' '||*data=='\t'||*data=='\n'||*data=='\r')data++;
 uint64_t remainder=0;char status[16];int consumed=0;
 if(sscanf(data,"%" SCNu64 " cli_cmd - %15s %n",&remainder,status,&consumed)!=2 || consumed<=0 ||
    remainder!=seq || strcmp(status,"SUCCESS") || data[consumed])return -EBADMSG;
 memcpy(out,bytes,count);return 0;
}
int y4_tmp75_decode(uint8_t config,const uint8_t bytes[2],int64_t *out)
{
 if(!bytes||!out)return -EINVAL;
 if(config&1u)return -EHOSTDOWN;
 unsigned raw=(unsigned)bytes[0]*256u+bytes[1];
 /* TI E2E 1582666: newer TMP75 reports 12 bits even with R1/R0=00.
  * Only the four bits outside the signed 12-bit data format are reserved. */
 if(raw&15u)return -EBADMSG;
 int64_t signed_raw=raw>=32768u?(int64_t)raw-65536:(int64_t)raw;
 int64_t value=signed_raw*1000/256;
 if(value< -40000||value>125000)return -ERANGE;
 *out=value;return 0;
}
static int exchange(const struct y4_call *call,volatile unsigned char *p,const char *command,char frame[4096],uint32_t *seq)
{
 int rc=y4_command_poll(call);if(rc)return rc;
 rc=layout(p);if(rc)return rc;
 uint32_t w=load(p,TX_W),r=load(p,TX_R);if(w>=TX_SIZE||r>=TX_SIZE)return -EPROTO;
 if(w!=r)return -EBUSY;
 size_t length=strlen(command);if(!length||length>=TX_SIZE)return -E2BIG;
 for(size_t i=0;i<length;i++)p[TX_DATA+(w+(uint32_t)i)%TX_SIZE]=(unsigned char)command[i];
 store(p,TX_W,(w+(uint32_t)length)%TX_SIZE);
 size_t used=0;frame[0]=0;
 for(;;){char chunk[1024];size_t n=0;rc=receive(p,chunk,sizeof(chunk),&n);if(rc)return rc;
  for(size_t i=0;i<n;i++){
   unsigned char c=(unsigned char)chunk[i];if(c=='\r'||!c)c='\n';
   if(c!='\n'&&c!='\t'&&(c<32u||c>126u)){
    int bad=snprintf(frame+used,4096u-used,"<invalid-byte-%02X>",c);
    (void)bad;return -EBADMSG;
   }
   if(used==4095u)return -EOVERFLOW;
   frame[used++]=(char)c;
  }frame[used]=0;int good=0;rc=terminal(frame,seq,&good);
  if(!rc)return good?0:-EREMOTEIO;
  if(rc!=-EAGAIN)return rc;
  rc=poll_delay(call);if(rc)return rc;
 }
}
static int read_at(const struct y4_call *call,struct y4_tmp75_pm_sample *out,unsigned address)
{
 if(!call||!out||!call->now_ms)return -EINVAL;
 memset(out,0,sizeof(*out));out->phase=1;
 int rc=y4_command_poll(call);if(rc)return rc;
 char frame[4096]={0};
 int lock=open("/run/y4-pm-tmp75.lock",O_RDWR|O_CREAT|O_NOFOLLOW|O_CLOEXEC,0600);
 if(lock<0)return -errno;
 struct stat st;if(fstat(lock,&st)||!S_ISREG(st.st_mode)||st.st_uid||st.st_nlink!=1u||(st.st_mode&077u)){rc=-EPERM;goto done;}
 while(flock(lock,LOCK_EX|LOCK_NB)){
  if(errno!=EAGAIN && errno!=EWOULDBLOCK){rc=-errno;goto done;}
  rc=poll_delay(call);if(rc)goto done;
 }
 int fd=open("/dev/mem",O_RDWR|O_SYNC|O_NOFOLLOW|O_CLOEXEC);if(fd<0){rc=-errno;goto done;}
 if(fstat(fd,&st)||!S_ISCHR(st.st_mode)||major(st.st_rdev)!=1u||minor(st.st_rdev)!=1u){rc=-EPERM;close(fd);goto done;}
 volatile unsigned char *p=mmap(NULL,WINDOW,PROT_READ|PROT_WRITE,MAP_SHARED,fd,(off_t)0x83bf3000u);
 if(close(fd)){rc=-errno;if(p!=MAP_FAILED)munmap((void *)p,WINDOW);goto done;}
 if(p==MAP_FAILED){rc=-errno;goto done;}
 out->phase=2;rc=layout(p);if(rc)goto mapped;
 /* Bound draining of old replies; the fresh fwver barrier pins a sequence.
  * Never reuse a NUL-terminated fragment or accept a command-success footer
  * as evidence that the I2C operation succeeded. */
 uint64_t quiet=call->now_ms(call->opaque);size_t drained=0;
 for(;;){char buffer[1024];size_t n=0;rc=receive(p,buffer,sizeof(buffer),&n);if(rc)goto mapped;
  drained+=n;if(drained>8192u){rc=-EOVERFLOW;goto mapped;}
  uint64_t now=call->now_ms(call->opaque);if(now<quiet){rc=-EIO;goto mapped;}
  if(n)quiet=now;else if(now-quiet>=50u)break;
  rc=poll_delay(call);if(rc)goto mapped;
 }
 out->phase=3;uint32_t seq=0;rc=exchange(call,p,"fwver\n",frame,&seq);if(rc)goto mapped;
 if(!strstr(frame,"MINI_PORT_")||!strstr(frame,"BuildTime")||seq>UINT32_MAX-2u){rc=-EPROTO;goto mapped;}
 out->phase=4;uint32_t expected=seq+1u;rc=exchange(call,p,address==0x48u?"i2c 3 R1 0x48 1\n":"i2c 3 R1 0x49 1\n",frame,&seq);if(rc)goto mapped;
 uint8_t config=0;rc=y4_tmp75_pm_parse(frame,expected,address,&config,1);if(rc)goto mapped;
 out->config=config;out->sequence=seq;
 out->phase=5;expected++;rc=exchange(call,p,address==0x48u?"i2c 3 R2 0x48 0\n":"i2c 3 R2 0x49 0\n",frame,&seq);if(rc)goto mapped;
 uint8_t bytes[2];rc=y4_tmp75_pm_parse(frame,expected,address,bytes,2);if(rc)goto mapped;
 out->sequence=seq;out->raw=(uint16_t)((unsigned)bytes[0]*256u+bytes[1]);out->phase=6;
 struct y4_tmp75_pm_sample sample={.sequence=seq,.config=config,.raw=out->raw,.phase=6};rc=y4_tmp75_decode(config,bytes,&sample.millicelsius);
 if(!rc){rc=y4_command_poll(call);if(!rc)*out=sample;}
 mapped: if(rc)snprintf(out->diagnostic,sizeof(out->diagnostic),"%.1023s",frame);
 if(munmap((void *)p,WINDOW)&&!rc)rc=-errno;
 done: if(close(lock)&&!rc)rc=-errno;return rc;
}

static int bounded_read(const struct y4_call *call,struct y4_tmp75_pm_sample *out,
 int (*acquire)(const struct y4_call *,struct y4_tmp75_pm_sample *,unsigned))
{
 int rc=acquire(call,out,0x48u);
 if(!out)return rc;
 out->attempts=1;out->first_error=rc;
 if(rc!=-EREMOTEIO)return rc;
 /* At most one fresh, bounded acquisition after an explicit I2C failure.
  * Never retry a write/reset, reuse old bytes, clear the first error, or
  * extend the caller deadline. An absent-address diagnostic is not retried. */
 for(unsigned i=0;i<10u;i++){int next=poll_delay(call);if(next)return next;}
 rc=acquire(call,out,0x48u);out->attempts=2;out->first_error=-EREMOTEIO;
 return rc;
}
int y4_tmp75_pm_read(const struct y4_call *call,struct y4_tmp75_pm_sample *out)
{ return bounded_read(call,out,read_at); }
int y4_tmp75_pm_absent_address_probe(const struct y4_call *call,struct y4_tmp75_pm_sample *out)
{ int rc=read_at(call,out,0x49u);if(out){out->attempts=1;out->first_error=rc;}return rc; }
