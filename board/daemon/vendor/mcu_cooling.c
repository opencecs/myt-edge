#include "y4_os_mcu.h"
#include "y4_os_cooling.h"
#include <errno.h>
int y4_mcu_cooling_apply(int fd,const struct y4_cooling_output *out)
{
    /* Invalidate first; incomplete I2C transactions cannot authorize low PWM. */
    int rc=y4_mcu_write_verified(fd,0x48,0);
    if(rc)return rc;
    if(!out||out->duty<50u||out->duty>100u||
       (out->valid!=0&&out->valid!=1)||(!out->valid&&out->duty!=100u)||
       (out->valid&&(out->decicelsius< -400||out->decicelsius>1250)))
        return -EINVAL;
    for(uint8_t i=0;i<4&&!rc;++i)
        rc=y4_mcu_write_verified(fd,(uint8_t)(1u+i),
            (Y4_INSTALLED_FAN_MASK&(1u<<i))?(uint8_t)out->duty:100);
    if(!rc&&out->valid){
        uint16_t temperature=(uint16_t)out->decicelsius;
        rc=y4_mcu_write_verified(fd,0x46,(uint8_t)temperature);
        if(!rc)rc=y4_mcu_write_verified(fd,0x47,(uint8_t)(temperature>>8));
        if(!rc)rc=y4_mcu_write_verified(fd,0x48,1);
    }
    return rc;
}

/* Display-only SoC publication; cannot grant temperature-based fan reduction. */
int y4_mcu_soc_display(int fd,int error,const struct y4_temperature_snapshot *s)
{
    int rc=y4_mcu_write_verified(fd,0x5d,0);
    if(rc)return rc;
    if(error||!s||s->scan_error||!s->soc_valid||s->soc_error||
       !s->soc_count||s->soc_max < -40000||s->soc_max > 125000)return 0;
    uint16_t t=(uint16_t)(int16_t)(s->soc_max/100);
    rc=y4_mcu_write_verified(fd,0x5b,(uint8_t)t);
    if(!rc)rc=y4_mcu_write_verified(fd,0x5c,(uint8_t)(t>>8));
    if(!rc)rc=y4_mcu_write_verified(fd,0x5d,1);
    return rc;
}

static uint8_t cooling_crc8(uint8_t token,const uint8_t data[7])
{
    uint8_t crc=0;
    for(unsigned i=0;i<8u;++i){
        crc^=i?data[i-1u]:token;
        for(unsigned bit=0;bit<8u;++bit)crc=(uint8_t)(((unsigned)crc<<1)^((crc&0x80u)?7u:0u));
    }
    return crc;
}
static int cooling_cancel(int fd,int cause)
{
    int safe=y4_mcu_write_verified(fd,0x48,0);
    return cause?cause:safe;
}
int y4_mcu_cooling_apply_for(int fd,const struct y4_mcu_identity *id,const struct y4_cooling_output *out)
{
    if(!id)return cooling_cancel(fd,-EINVAL);
    if(!(id->version[0]==0u&&id->version[1]==1u&&id->version[2]>=26u))
        return y4_mcu_cooling_apply(fd,out);
    if(!out||out->duty<15u||out->duty>100u||
       (out->valid!=0&&out->valid!=1)||(!out->valid&&out->duty!=100u)||
       (out->valid&&(out->decicelsius< -400||out->decicelsius>1250)))
        return cooling_cancel(fd,-EINVAL);
    if(!out->valid)return cooling_cancel(fd,0);
    uint8_t previous,token,payload[7],status[2];
    int rc=y4_mcu_read(fd,0x25,&previous,1);
    if(rc)return cooling_cancel(fd,rc);
    token=previous==255u?1u:(uint8_t)(previous+1u);
    for(unsigned i=0;i<4u;++i)payload[i]=(uint8_t)out->duty;
    uint16_t temperature=(uint16_t)out->decicelsius;
    payload[4]=(uint8_t)temperature;payload[5]=(uint8_t)(temperature>>8);payload[6]=1;
    rc=y4_mcu_write_verified(fd,0x25,token);
    for(unsigned i=0;!rc&&i<7u;++i)rc=y4_mcu_write_verified(fd,(uint8_t)(0x26u+i),payload[i]);
    if(!rc)rc=y4_mcu_write_verified(fd,0x2d,cooling_crc8(token,payload));
    if(!rc)rc=y4_mcu_write_verified(fd,0x2e,token);
    if(!rc)rc=y4_mcu_read(fd,0x2e,status,2);
    if(!rc&&(status[0]!=token||status[1]!=2u))rc=-EPROTO;
    return rc?cooling_cancel(fd,rc):0;
}
