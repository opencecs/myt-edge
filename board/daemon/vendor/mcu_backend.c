#include "y4_os_mcu_backend.h"
#include <errno.h>
#include <string.h>
#include <stdint.h>
static uint32_t le32(const unsigned char *p){return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24;}
static int signed16(const unsigned char *p){unsigned v=(unsigned)p[0]|(unsigned)p[1]<<8;return v>=32768u?(int)v-65536:(int)v;}
static int overlap(const void *a,size_t n,const void *b,size_t m){uintptr_t x=(uintptr_t)a,y=(uintptr_t)b;return x<=y?y-x<n:x-y<m;}
int y4_mcu_backend_decode(const void *raw,size_t n,struct y4_mcu_backend_frame *out)
{
 if(!raw||!out)return -EINVAL;
 if(n!=Y4_MCU_BACKEND_BYTES)return -EPROTO;
 if(overlap(raw,n,out,sizeof *out))return -EINVAL;
 const unsigned char *b=raw;
 if(b[0]&1u)return -EAGAIN;
 if(b[1]!=1u)return -EOPNOTSUPP;
 if((b[2]&~3u)||(b[3]&128u))return -EPROTO;
 struct y4_mcu_backend_frame f={.sequence=b[0],.build=b[2],.flags=b[3],
  .spi_hz=le32(b+4),.dma_completed=le32(b+8),.error_events=le32(b+12),
  .success_bytes=le32(b+16),.spi_current=signed16(b+20),.spi_last=signed16(b+22),
  .pwm_status=signed16(b+24),.pwm_hz=(unsigned)b[26]|(unsigned)b[27]<<8};
 if(f.spi_current>0||f.spi_current< -4095||f.spi_last>0||f.spi_last< -4095||f.pwm_status>1||f.pwm_status< -4095)return -EPROTO;
 if(!(f.flags&64u))return -ENODATA;
 if(!(f.build&1u)&&((f.flags&31u)||f.spi_hz||f.dma_completed||f.error_events||f.success_bytes||f.spi_current||f.spi_last))return -EPROTO;
 if(!(f.build&2u)&&(f.pwm_status||f.pwm_hz))return -EPROTO;
 if((f.flags&6u)&&!(f.flags&1u))return -EPROTO;
 if((f.flags&16u)&&!(f.flags&8u))return -EPROTO;
 if(!!(f.flags&1u)!=!!f.spi_hz)return -EPROTO;
 if((f.pwm_status==1)!=!!f.pwm_hz)return -EPROTO;
 int saturated=f.dma_completed==UINT32_MAX||f.error_events==UINT32_MAX||f.success_bytes==UINT32_MAX;
 if(!!(f.flags&32u)!=saturated)return -EPROTO;
 *out=f;return 0;
}
int y4_mcu_backend_frame_encode(const struct y4_mcu_backend_frame *f,void *raw,size_t n)
{
 if(!f||!raw||n!=Y4_MCU_BACKEND_BYTES)return -EINVAL;
 if(overlap(f,sizeof *f,raw,n))return -EINVAL;
 if(f->spi_current< -4095||f->spi_current>0||f->spi_last< -4095||f->spi_last>0||f->pwm_status< -4095||f->pwm_status>1||f->pwm_hz>65535u)return -EINVAL;
 unsigned char wire[Y4_MCU_BACKEND_BYTES]={0};wire[0]=f->sequence;wire[1]=1;wire[2]=f->build;wire[3]=f->flags;
 uint32_t counters[]={f->spi_hz,f->dma_completed,f->error_events,f->success_bytes};
 for(unsigned i=0;i<4u;++i)for(unsigned k=0;k<4u;++k)wire[4u+4u*i+k]=(unsigned char)(counters[i]>>(8u*k));
 int statuses[]={f->spi_current,f->spi_last,f->pwm_status};
 for(unsigned i=0;i<3u;++i){uint16_t v=(uint16_t)statuses[i];wire[20u+2u*i]=(unsigned char)v;wire[21u+2u*i]=(unsigned char)(v>>8);}
 wire[26]=(unsigned char)f->pwm_hz;wire[27]=(unsigned char)(f->pwm_hz>>8);
 struct y4_mcu_backend_frame check;int rc=y4_mcu_backend_decode(wire,sizeof wire,&check);
 if(rc)return rc;
 memcpy(raw,wire,sizeof wire);return 0;
}
int y4_mcu_backend_track(struct y4_mcu_backend_tracker *t,uint64_t now,int status,
 const struct y4_mcu_backend_frame *frame,struct y4_mcu_backend_snapshot *out)
{
 if(!t||!out||!now||now==UINT64_MAX||status>0||status< -4095||(!status&&!frame))return -EINVAL;
 if(overlap(t,sizeof *t,out,sizeof *out)||(frame&&(overlap(t,sizeof *t,frame,sizeof *frame)||overlap(frame,sizeof *frame,out,sizeof *out))))return -EINVAL;
 if(t->seeded!=0&&t->seeded!=1)return -EINVAL;
 if(t->previous_read_ms>now||t->advance_ms>now)return -ESTALE;
 if(!status){unsigned char wire[Y4_MCU_BACKEND_BYTES];int rc=y4_mcu_backend_frame_encode(frame,wire,sizeof wire);if(rc)return rc;}
 struct y4_mcu_backend_tracker next=*t;
 struct y4_mcu_backend_snapshot s={.sampled_ms=now,.status=status};
 if(status){memset(&next,0,sizeof next);}
 else if(!t->seeded){next.seeded=1;next.sequence=frame->sequence;next.previous_read_ms=now;s.status=-EAGAIN;}
 else{
  if(frame->sequence!=t->sequence){next.sequence=frame->sequence;next.advance_ms=t->previous_read_ms;}
  next.previous_read_ms=now;
  if(!next.advance_ms)s.status=-EAGAIN;
  else if(now-next.advance_ms>=Y4_MCU_BACKEND_MAX_AGE)s.status=-ESTALE;
  else{s.sampled_ms=next.advance_ms;s.frame=*frame;}
 }
 *t=next;*out=s;return 0;
}
