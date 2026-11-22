#ifndef Y4_SLOT_IDENTITY_H
#define Y4_SLOT_IDENTITY_H
/* Stable display codes shared by Linux and the MCU. Exact board driver IDs;
 * identification does not prove that a device driver/application is ready. */
#include "protocol.h"
static inline unsigned y4_slot_identify(unsigned vendor,unsigned device,unsigned cls)
{
 if(vendor==0x1d87 && device==0x182a)return Y4_SLOT_RK182X;
 if((vendor==0x1f6b || vendor==0x1ec8) && device==0x0c00)return Y4_SLOT_M50;
 if(cls==0x010802)return Y4_SLOT_NVME;
 return Y4_SLOT_PCIE;
}
static inline const char *y4_slot_type_name(unsigned type)
{
 switch(type){case Y4_SLOT_NONE:return "none";case Y4_SLOT_NVME:return "nvme";
 case Y4_SLOT_RK182X:return "rk182x";case Y4_SLOT_M50:return "m50";
 default:return "pcie";}
}
#endif
