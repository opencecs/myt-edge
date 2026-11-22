#include "y4_os_mcu_uid_snapshot.h"
#include "y4_os_checksum.h"
#include <errno.h>
#include <string.h>
static void put(unsigned char *p,uint64_t v,unsigned n){for(unsigned i=0;i<n;++i)p[i]=(unsigned char)(v>>(8u*i));}
static uint64_t get(const unsigned char *p,unsigned n){uint64_t v=0;for(unsigned i=0;i<n;++i)v|=(uint64_t)p[i]<<(8u*i);return v;}
static int valid(const struct y4_mcu_uid_snapshot *s){if(!s||!s->sampled_ms||s->status>0||s->status< -4095)return 0;unsigned any=0,not_ff=0;for(unsigned i=0;i<12;++i){any|=s->uid[i];not_ff|=(unsigned)(s->uid[i]^255u);}return s->status?!any:any&&not_ff;}
int y4_mcu_uid_snapshot_encode(const struct y4_mcu_uid_snapshot *s,void *out,size_t n){if(!out||n!=64u||!valid(s))return -EINVAL;unsigned char p[64]={0};memcpy(p,"Y4UIDS01",8);put(p+8,s->sampled_ms,8);put(p+16,(uint32_t)(-s->status),4);memcpy(p+24,s->uid,12);put(p+60,y4_crc32c(p,60),4);memcpy(out,p,64);return 0;}
int y4_mcu_uid_snapshot_decode(const void *raw,size_t n,uint64_t now,struct y4_mcu_uid_snapshot *out){if(!raw||!out)return -EINVAL;if(n!=64u)return -EPROTO;const unsigned char *p=raw;if(memcmp(p,"Y4UIDS01",8)||get(p+16,4)>4095u||get(p+60,4)!=y4_crc32c(p,60))return -EPROTO;for(unsigned i=20;i<24;++i)if(p[i])return -EPROTO;for(unsigned i=36;i<60;++i)if(p[i])return -EPROTO;struct y4_mcu_uid_snapshot s={.sampled_ms=get(p+8,8),.status=-(int)get(p+16,4)};memcpy(s.uid,p+24,12);if(!valid(&s))return -EPROTO;if(now<s.sampled_ms||now-s.sampled_ms>3000u)return -ESTALE;*out=s;return 0;}
