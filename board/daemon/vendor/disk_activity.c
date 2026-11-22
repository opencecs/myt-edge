#define _XOPEN_SOURCE 700
#include "y4_os_disk_activity.h"
#include <dirent.h>
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
static int path_join(char path[PATH_MAX],const char *base,const char *name,const char *leaf)
{
    int n=snprintf(path,PATH_MAX,"%s/%s/%s",base,name,leaf);
    return n<0||(unsigned)n>=PATH_MAX?-ENAMETOOLONG:0;
}
int y4_disk_counters_read(const char *root,struct y4_disk_counters *out)
{
    if(!root||!out)return -EINVAL;
    DIR *dir=opendir(root);if(!dir)return -errno;
    struct y4_disk_counters pending={0};struct dirent *entry;int error=0;
    while((errno=0,entry=readdir(dir))!=NULL){
        if(entry->d_name[0]=='.')continue;
        char path[PATH_MAX],real[PATH_MAX];
        error=path_join(path,root,entry->d_name,"");if(error)break;
        if(!realpath(path,real)){error=-errno;break;}
        /* Same slot mapping as slots.c: match the ":c2:0X.0/" suffix without
         * the PCI domain, which depends on controller probe order. */
        const char *ports[]={":c2:00.0/",":c2:03.0/",":c2:06.0/",":c2:09.0/"};
        int slot=-1;for(unsigned i=0;i<4;++i)if(strstr(real,ports[i]))slot=(int)i;
        if(slot<0)continue;
        error=path_join(path,root,entry->d_name,"partition");if(error)break;
        struct stat st;if(stat(path,&st)==0)continue;
        if(errno!=ENOENT){error=-errno;break;}
        error=path_join(path,root,entry->d_name,"diskseq");if(error)break;
        FILE *file=fopen(path,"r");if(!file){error=-errno;break;}
        uint64_t sequence=0;int ok=fscanf(file,"%" SCNu64,&sequence);fclose(file);
        if(ok!=1||!sequence){error=-EPROTO;break;}
        error=path_join(path,root,entry->d_name,"stat");if(error)break;
        file=fopen(path,"r");if(!file){error=-errno;break;}
        uint64_t reads,merged,sectors,ticks,writes;
        ok=fscanf(file,"%" SCNu64 " %" SCNu64 " %" SCNu64 " %" SCNu64 " %" SCNu64,&reads,&merged,&sectors,&ticks,&writes);fclose(file);
        if(ok!=5){error=-EPROTO;break;}
        if(reads>UINT64_MAX-writes||pending.operations[slot]>UINT64_MAX-reads-writes){error=-EOVERFLOW;break;}
        pending.operations[slot]+=reads+writes;
        /* Commutative fingerprint: sysfs iteration order must not look like replacement. */
        uint64_t fingerprint=UINT64_C(14695981039346656037);
        for(const unsigned char *p=(const unsigned char *)real;*p;++p)fingerprint=(fingerprint^*p)*UINT64_C(1099511628211);
        for(unsigned i=0;i<8;++i)fingerprint=(fingerprint^((sequence>>(i*8u))&255u))*UINT64_C(1099511628211);
        pending.generation[slot]^=fingerprint;
        pending.present|=(uint8_t)(1u<<(unsigned)slot);
    }
    if(!error&&errno)error=-errno;
    closedir(dir);if(!error)*out=pending;return error;
}
uint8_t y4_disk_activity_step(struct y4_disk_activity *state,const struct y4_disk_counters *sample)
{
    if(!state)return 0;
    if(!sample){state->valid=0;return 0;}
    uint8_t active=0;
    for(unsigned i=0;i<4;++i){
        uint8_t bit=(uint8_t)(1u<<i);
        if(state->valid&&(sample->present&state->previous.present&bit)&&
           sample->generation[i]==state->previous.generation[i]&&
           sample->operations[i]>state->previous.operations[i])active|=bit;
    }
    state->previous=*sample;state->valid=1;return active;
}
