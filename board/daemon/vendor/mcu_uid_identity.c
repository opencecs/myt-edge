#include "y4_os_mcu.h"
#include <errno.h>
#include <string.h>
int y4_mcu_board_identity_from_uid(const uint8_t uid[12],uint8_t out[16])
{
 if(!uid||!out)return -EINVAL;
 unsigned nonzero=0,nonff=0;
 for(unsigned i=0;i<12u;++i){nonzero|=uid[i];nonff|=(unsigned)(uid[i]^255u);}
 if(!nonzero||!nonff)return -ENODATA;
 uint8_t result[16];memcpy(result,"Y4MC",4);memcpy(result+4,uid,12);memcpy(out,result,sizeof(result));return 0;
}
