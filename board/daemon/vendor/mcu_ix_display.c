#define _POSIX_C_SOURCE 200809L
#include "y4_os_mcu_ix_display.h"
#include "y4_os_ix_thermal.h"
#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <time.h>

#define IX_BEGIN 0x18u
#define IX_DATA 0x19u
#define IX_CRC 0x1fu
#define IX_COMMIT 0x20u
#define IX_PUBLISHED 0x0au
#define IX_MAX_AGE_MS UINT64_C(15000)

static int supported(const struct y4_mcu_identity *id)
{
 return id->version[0]==0u && id->version[1]==1u && id->version[2]>=28u;
}
static int now_ns(uint64_t *out)
{
 struct timespec t;
 if(clock_gettime(CLOCK_MONOTONIC,&t))return errno?-errno:-EIO;
 if(t.tv_sec<0||t.tv_nsec<0||t.tv_nsec>=1000000000L ||
    (uint64_t)t.tv_sec>(UINT64_MAX-(uint64_t)t.tv_nsec)/UINT64_C(1000000000))return -ERANGE;
 *out=(uint64_t)t.tv_sec*UINT64_C(1000000000)+(uint64_t)t.tv_nsec;
 return *out?0:-ERANGE;
}
static uint8_t crc8(uint8_t token,const uint8_t payload[6])
{
 uint8_t crc=0;
 for(unsigned i=0;i<7u;i++) {
  crc^=i?payload[i-1u]:token;
  for(unsigned bit=0;bit<8u;bit++)crc=(uint8_t)(((unsigned)crc<<1)^((crc&0x80u)?7u:0u));
 }
 return crc;
}
static int transport(int rc)
{return rc>0||rc< -4095?-EPROTO:rc;}
static void put16(uint8_t *p,uint16_t value)
{p[0]=(uint8_t)value;p[1]=(uint8_t)(value>>8);}
static uint64_t age_ms(const struct y4_ix_thermal_sample *s,uint64_t now)
{
 if(!s->sampled_ns||s->sampled_ns>now)return UINT64_MAX;
 uint64_t elapsed=now-s->sampled_ns;
 uint64_t age=elapsed/UINT64_C(1000000)+(elapsed%UINT64_C(1000000)!=0u);
 return age>s->age_ms?age:s->age_ms;
}
static int sensor_error(const struct y4_ix_thermal_sample *s,uint64_t now,uint64_t age)
{
 if(s->status>0||s->status< -4095||s->temperature_error>0||
    s->temperature_error< -4095||(s->valid!=0&&s->valid!=1))return -EPROTO;
 if(!s->sampled_ns||!s->completed_ns||s->completed_ns<s->sampled_ns||
    s->completed_ns>now||age>=IX_MAX_AGE_MS)return -ESTALE;
 if(s->status)return s->status;
 if(s->temperature_error)return s->temperature_error;
 if(!s->valid)return -ENODATA;
 if(s->millicelsius< -40000||s->millicelsius>125000)return -ERANGE;
 return 0;
}
int y4_mcu_ix_display(int fd,const struct y4_mcu_identity *id,
                      const struct y4_ix_thermal_sample *sample)
{
 uint8_t prior=0,payload[6],published[6];uint64_t now,age;int rc,error;
 if(!id)return -EINVAL;
 if(!supported(id))return -EOPNOTSUPP;
 if(fd<0||!sample)return -EINVAL;
 rc=transport(y4_mcu_read(fd,IX_BEGIN,&prior,1));if(rc)return rc;
 uint8_t token=prior==UINT8_MAX?1u:(uint8_t)(prior+1u);
 rc=transport(y4_mcu_write_verified(fd,IX_BEGIN,token));if(rc)return rc;
 /* BEGIN is acknowledged before measuring age. MCU independently subtracts
  * its entire BEGIN-to-COMMIT interval, making this conservatively old. */
 rc=now_ns(&now);if(rc)return rc;
 age=age_ms(sample,now);error=sensor_error(sample,now,age);
 uint16_t wire_age=age>UINT16_MAX?UINT16_MAX:(uint16_t)age;
 int16_t temperature=error?0:(int16_t)(sample->millicelsius/100);
 put16(payload,(uint16_t)temperature);
 put16(payload+2,(uint16_t)(int16_t)error);
 put16(payload+4,wire_age);
 for(unsigned i=0;i<6u;i++) {
  rc=transport(y4_mcu_write_verified(fd,(uint8_t)(IX_DATA+i),payload[i]));if(rc)return rc;
 }
 rc=transport(y4_mcu_write_verified(fd,IX_CRC,crc8(token,payload)));if(rc)return rc;
 /* Do not publish a formerly-valid payload that expired during transfer.
  * Abandon the pending frame; never restart, rewrite bytes or reset MCU. */
 if(!error) {
  uint64_t before_commit;
  rc=now_ns(&before_commit);if(rc)return rc;
  if(before_commit<now||age_ms(sample,before_commit)>=IX_MAX_AGE_MS)return -ESTALE;
 }
 rc=transport(y4_mcu_write_verified(fd,IX_COMMIT,token));if(rc)return rc;
 rc=transport(y4_mcu_read(fd,IX_PUBLISHED,published,sizeof published));if(rc)return rc;
 if(published[0]!=payload[0]||published[1]!=payload[1]||
    published[2]!=payload[2]||published[3]!=payload[3]||
    published[4]!=(uint8_t)(error==0)||published[5]!=2u)return -EPROTO;
 return 0;
}
