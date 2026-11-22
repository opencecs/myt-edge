/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include "y4_display.h"
#include <errno.h>
#include <linux/i2c-dev.h>
#include <linux/i2c.h>
#include <stddef.h>
#include <string.h>
#include <sys/ioctl.h>

static uint8_t crc_byte(uint8_t crc,uint8_t byte)
{
    crc^=byte;
    for(unsigned i=0;i<8u;i++)
        crc=(uint8_t)(((unsigned)crc<<1)^((crc&0x80u)?7u:0u));
    return crc;
}
static void put16(uint8_t *p,unsigned value)
{
    p[0]=(uint8_t)value;p[1]=(uint8_t)(value>>8);
}
static int address_valid(const uint8_t ip[4])
{
    return ip[0]!=0u&&ip[0]<224u&&ip[0]!=127u;
}
int y4_display_encode(const struct y4_display_input *s,uint8_t token,
                      uint8_t frame[Y4_VIEW_FRAME_LEN])
{
    if(!s||!frame||!token)return -EINVAL;
    if((s->physical_valid_mask&~7u)||(s->physical_present_mask&~s->physical_valid_mask)||
       (s->physical_carrier_mask&~s->physical_present_mask)||(s->ipv4_valid_mask&~3u)||
       s->default_egress>VIEW_EGRESS_AMBIGUOUS||
       s->initialization_state>VIEW_INIT_CANCELLED||s->attempt>5u||
       (s->max_attempts&&s->max_attempts!=5u)||s->initialization_valid>1u||
       s->initialization_error>0||s->initialization_error < -4095||
       s->network_error>0||s->network_error < -4095||
       s->bond_state>VIEW_BOND_LACP_MULTI||(s->bond_active_mask&~6u)||
       (s->bond_lacp_mask&~6u))return -EINVAL;
    if(s->bond_state==VIEW_BOND_LACP_MULTI&&
       (s->bond_active_mask!=6u||s->bond_lacp_mask!=6u))return -EINVAL;
    if(s->bond_state==VIEW_BOND_LACP_SINGLE&&
       ((s->bond_active_mask!=2u&&s->bond_active_mask!=4u)||
        s->bond_lacp_mask!=s->bond_active_mask))return -EINVAL;
    uint8_t out[Y4_VIEW_FRAME_LEN]={0};
    out[0]=Y4_VIEW_SCHEMA;out[1]=s->physical_valid_mask;
    out[2]=s->physical_present_mask;out[3]=s->physical_carrier_mask;
    out[6]=s->ipv4_valid_mask;out[7]=s->default_egress;
    for(unsigned i=0;i<2u;i++){
        if(s->logical_state[i]>VIEW_NET_INITIALIZING||s->prefix_length[i]>32u)
            return -EINVAL;
        out[4u+i]=s->logical_state[i];
        if(s->ipv4_valid_mask&(1u<<i)){
            unsigned carrier=i?(s->physical_carrier_mask&6u):(s->physical_carrier_mask&1u);
            if(s->logical_state[i]!=VIEW_NET_READY||!carrier||
               !address_valid(s->ipv4[i])||!s->prefix_length[i])return -EINVAL;
            memcpy(out+8u+4u*i,s->ipv4[i],4u);
            out[27u+i]=s->prefix_length[i];
        }else if(s->logical_state[i]==VIEW_NET_READY)return -EINVAL;
    }
    out[18]=5u;out[19]=s->bond_state;
    out[29]=s->bond_active_mask;out[30]=s->bond_lacp_mask;
    if(s->initialization_valid){
        out[16]=s->initialization_state;out[17]=s->attempt;
        put16(out+20,(unsigned)(uint16_t)(int16_t)s->initialization_error);
        out[26]=Y4_VIEW_INIT_VALID;
    }
    put16(out+22,s->sample_age_ms);
    put16(out+24,(unsigned)(uint16_t)(int16_t)s->network_error);
    uint8_t crc=crc_byte(0,token);
    for(unsigned i=0;i<Y4_VIEW_PAYLOAD_LEN;i++)crc=crc_byte(crc,out[i]);
    out[Y4_VIEW_PAYLOAD_LEN]=crc;
    memcpy(frame,out,sizeof out);
    return 0;
}
static int read_register(int fd,uint8_t reg,uint8_t *out)
{
    struct i2c_msg messages[2]={
        {.addr=Y4_ADDR,.flags=0,.len=1,.buf=&reg},
        {.addr=Y4_ADDR,.flags=I2C_M_RD,.len=1,.buf=out}
    };
    struct i2c_rdwr_ioctl_data request={.msgs=messages,.nmsgs=2};
    int result=ioctl(fd,I2C_RDWR,&request);
    return result<0?-errno:result==2?0:-EIO;
}
static int write_register(int fd,uint8_t reg,uint8_t value)
{
    uint8_t bytes[2]={reg,value};
    struct i2c_msg message={.addr=Y4_ADDR,.flags=0,.len=2,.buf=bytes};
    struct i2c_rdwr_ioctl_data request={.msgs=&message,.nmsgs=1};
    int result=ioctl(fd,I2C_RDWR,&request);
    return result<0?-errno:result==1?0:-EIO;
}
static int write_checked(int fd,uint8_t reg,uint8_t value)
{
    int status=write_register(fd,reg,value);
    uint8_t observed=0;
    if(!status)status=read_register(fd,reg,&observed);
    return status?status:observed==value?0:-EIO;
}
int y4_display_publish(int fd,const struct y4_display_input *s)
{
    if(fd<0||!s)return -EINVAL;
    uint8_t schema=0,previous=0,status=0,committed=0;
    int rc=read_register(fd,R_VIEW_SCHEMA,&schema);
    if(rc)return rc;
    if(schema!=Y4_VIEW_SCHEMA)return -EPROTONOSUPPORT;
    rc=read_register(fd,R_VIEW_BEGIN,&previous);
    if(rc)return rc;
    uint8_t token=previous==255u?1u:(uint8_t)(previous+1u);
    uint8_t frame[Y4_VIEW_FRAME_LEN];
    rc=y4_display_encode(s,token,frame);
    if(rc)return rc;
    rc=write_checked(fd,R_VIEW_BEGIN,token);
    if(!rc)rc=read_register(fd,R_VIEW_STATUS,&status);
    if(!rc&&status!=VIEW_PENDING)rc=-EPROTO;
    for(unsigned i=0;!rc&&i<Y4_VIEW_FRAME_LEN;i++)
        rc=write_checked(fd,R_VIEW_DATA,frame[i]);
    if(!rc)rc=write_register(fd,R_VIEW_COMMIT,token);
    if(!rc)rc=read_register(fd,R_VIEW_COMMIT,&committed);
    if(!rc)rc=read_register(fd,R_VIEW_STATUS,&status);
    if(!rc&&committed!=token)rc=-EPROTO;
    if(!rc&&status==VIEW_EXPIRED)rc=-ESTALE;
    else if(!rc&&status!=VIEW_COMMITTED)rc=-EPROTO;
    if(rc&&rc!=-ESTALE){
        /* Invalid BEGIN cancels only staging, never renews an old sample. */
        (void)write_register(fd,R_VIEW_BEGIN,0);
    }
    return rc;
}
