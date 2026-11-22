#include "y4_os_mcu_backend.h"
#include "y4_os_mcu.h"
#include <errno.h>
static int read_frame(int fd,struct y4_mcu_backend_frame *out)
{
 if(fd<0||!out)return -EINVAL;
 unsigned char first,last,wire[Y4_MCU_BACKEND_BYTES];
 int rc=y4_mcu_read(fd,Y4_MCU_BACKEND_REGISTER,&first,1);
 if(rc)return rc>0?-EIO:rc;
 if(first&1u)return -EAGAIN;
 rc=y4_mcu_read(fd,Y4_MCU_BACKEND_REGISTER,wire,sizeof wire);
 if(rc)return rc>0?-EIO:rc;
 rc=y4_mcu_read(fd,Y4_MCU_BACKEND_REGISTER,&last,1);
 if(rc)return rc>0?-EIO:rc;
 if(first!=last||first!=wire[0]||(last&1u))return -EAGAIN;
 return y4_mcu_backend_decode(wire,sizeof wire,out);
}

int y4_mcu_backend_observe(int fd,const struct y4_mcu_identity *id,struct y4_mcu_backend_frame *out)
{
 if(fd<0||!id||!out)return -EINVAL;
 if(id->version[0]!=0u||id->version[1]!=1u||id->version[2]<24u)return -EOPNOTSUPP;
 return read_frame(fd,out);
}
