#define _GNU_SOURCE
#include "ix_bar_probe.h"
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stddef.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static const uint32_t portnum[5]={12u,0u,3u,6u,9u};
static const uint32_t cfgbase[5] = {0x8000u,0x20000u,0x23000u,0x38000u,0x3b000u};
static uint32_t le32(const unsigned char *p)
{
    return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);
}
static uint64_t ms_now(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC,&t)!=0) return 0;
    return (uint64_t)t.tv_sec*1000u+(uint64_t)t.tv_nsec/1000000u;
}
static int poll_status(ix_bar_probe_poll fn, void *ctx)
{
    int r=fn(ctx);
    return (r>0||r< -4095)?-EPROTO:r;
}
static int read_word(volatile const unsigned char *bar,uint32_t off,
                     ix_bar_probe_poll fn,void *ctx,ix_bar_probe_result *r,uint32_t *v)
{
    int e;
    if ((off&3u)!=0u||off>IX_BAR_PROBE_BYTES-4u) return -ERANGE;
    r->failed_offset=off;
    e=poll_status(fn,ctx); if(e) return e;
    /* Exactly one volatile aligned 32-bit load, never memcpy or 64-bit access.
     * Little endian decoded explicitly even on a big-endian build host. */
    uint32_t native=*(volatile const uint32_t *)(bar+off);
    unsigned char bytes[4]; memcpy(bytes,&native,4);
    *v=le32(bytes);
    r->reads_completed++;
    return poll_status(fn,ctx);
}
static int read_config(int fd,unsigned char bytes[256],
                       ix_bar_probe_poll fn,void *ctx)
{
    for(unsigned off=0;off<256u;off+=4u){
        int e=poll_status(fn,ctx);
        if(e)return e;
        ssize_t n=pread(fd,bytes+off,4,(off_t)off);
        if(n<0)return -errno; /* including EINTR: no implicit retry */
        if(n!=4)return -EIO;
        e=poll_status(fn,ctx);
        if(e)return e;
    }
    return 0;
}
static int cap_find(const unsigned char c[256],uint32_t *offset)
{
    unsigned char seen[256]={0};
    unsigned n=0, p=c[0x34];
    if((c[0x06]&0x10u)==0u)return -ENOTSUP;
    while(p){
        if(p<0x40u||p>0xfcu||(p&3u)||seen[p]||n++>=48u)return -EPROTO;
        seen[p]=1;
        if(c[p]==0x10u){
            if(p>0xecu)return -EPROTO;
            *offset=p; return 0;
        }
        p=c[p+1u];
    }
    return -ENOTSUP;
}
static int child_probe(int barfd,const int cfds[5],ix_bar_probe_poll fn,void *ctx,
                       ix_bar_probe_result *r)
{
    unsigned char c[5][256];
    uint32_t cap[5], v;
    int e;
    r->stage=1;
    for(unsigned i=0;i<5u;i++){
        e=read_config(cfds[i],c[i],fn,ctx); if(e)return e;
        if(le32(c[i])!=0x80241ec8u)return -ENODEV;
        if((c[i][0x0e]&0x7fu)!=1u||c[i][0x0b]!=6u||c[i][0x0a]!=4u)return -ENODEV;
        e=cap_find(c[i],&cap[i]); if(e)return e;
        /* Require USP/DSP port role from live PCIe capability. */
        unsigned role=((unsigned)c[i][cap[i]+2u]>>4)&0xfu;
        if(role!=(i==0u?5u:6u))return -ENODEV;
    }
    r->stage=2;
    struct stat st;
    if(fstat(barfd,&st)!=0)return -errno;
    if(st.st_size!=(off_t)IX_BAR_PROBE_BYTES)return -ERANGE;
    volatile const unsigned char *bar=mmap(NULL,IX_BAR_PROBE_BYTES,PROT_READ,MAP_SHARED,barfd,0);
    if(bar==MAP_FAILED)return -errno;
    r->stage=3;
    for(unsigned i=0;i<5u;i++){
        e=read_word(bar,cfgbase[i],fn,ctx,r,&v); if(e)goto done;
        r->ids[i]=v; if(v!=le32(c[i])){e=-EUCLEAN;goto done;}
        e=read_word(bar,cfgbase[i]+8u,fn,ctx,r,&v); if(e)goto done;
        r->classes[i]=v; if(v!=le32(c[i]+8u)){e=-EUCLEAN;goto done;}
        e=read_word(bar,cfgbase[i]+0x18u,fn,ctx,r,&v);if(e)goto done;
        if(v!=le32(c[i]+0x18u)){e=-EUCLEAN;goto done;}
        e=read_word(bar,cfgbase[i]+cap[i]+0x0cu,fn,ctx,r,&v);if(e)goto done;
        if(v!=le32(c[i]+cap[i]+0x0cu)){e=-EUCLEAN;goto done;}
        e=read_word(bar,cfgbase[i]+cap[i],fn,ctx,r,&v);if(e)goto done;
        r->pcie_headers[i]=v; if(v!=le32(c[i]+cap[i])){e=-EUCLEAN;goto done;}
        /* Link control is stable; negotiated Link Status may change between
         * config and BAR snapshots. Compare only speed/width bits plus control
         * and role; report the actual dword even when mismatch stops probe. */
        e=read_word(bar,cfgbase[i]+cap[i]+0x10u,fn,ctx,r,&v);if(e)goto done;
        r->link_dwords[i]=v;
        if((v&0x03ffffffu)!=(le32(c[i]+cap[i]+0x10u)&0x03ffffffu)){
            e=-EAGAIN;goto done;
        }
    }
    r->mapping_verified=1;
    r->stage=4;
    for(unsigned i=0;i<5u;i++){
        e=read_word(bar,0x2cu+0x80u*portnum[i],fn,ctx,r,&r->training_status[i]);
        if(e)goto done;
        if(r->training_status[i]==UINT32_MAX){e=-EIO;goto done;}
    }
    e=read_word(bar,0x1010u,fn,ctx,r,&r->raw_temperature);if(e)goto done;
    e=read_word(bar,0x1c18u,fn,ctx,r,&r->calibration);if(e)goto done;
    e=read_word(bar,0x184cu,fn,ctx,r,&r->management_mode);if(e)goto done;
    e=read_word(bar,0x1000u,fn,ctx,r,&r->control0);if(e)goto done;
    e=read_word(bar,0x1004u,fn,ctx,r,&r->control1);if(e)goto done;
    r->stage=5;r->failed_offset=0;
done:
    (void)munmap((void *)bar,IX_BAR_PROBE_BYTES);
    return e;
}
static void child_send(int fd,const ix_bar_probe_result *r)
{
    const unsigned char *p=(const unsigned char *)r;
    size_t n=0;
    while(n<sizeof(*r)){
        ssize_t k=write(fd,p+n,sizeof(*r)-n);
        if(k<0){if(errno==EINTR)continue;break;}
        n+=(size_t)k;
    }
}
static int reap_bounded(pid_t pid,int kill_child,pid_t *unreaped)
{
    int ws;
    if(kill_child)(void)kill(pid,SIGKILL);
    uint64_t start=ms_now();
    for(;;){
        pid_t w=waitpid(pid,&ws,WNOHANG);
        if(w==pid)return 0;
        if(w<0){if(errno==EINTR)continue;return errno==ECHILD?0:-errno;}
        uint64_t now=ms_now();
        if(!now||now-start>=100u){*unreaped=pid;return -EINPROGRESS;}
        struct timespec delay={0,1000000};(void)nanosleep(&delay,NULL);
    }
}
int ix_bar_probe_run(int barfd,const int cfds[5],ix_bar_probe_poll fn,void *ctx,
                     ix_bar_probe_result *out)
{
    int p[2], e=0;
    if(!out)return -EINVAL;
    memset(out,0,sizeof(*out));
    if(barfd<0||!cfds||!fn){out->status=-EINVAL;return out->status;}
    for(unsigned i=0;i<5u;i++)if(cfds[i]<0){out->status=-EINVAL;return out->status;}
    uint64_t start=ms_now();if(!start){out->status=-EIO;return out->status;}
    if(pipe2(p,O_CLOEXEC|O_NONBLOCK)!=0){out->status=-errno;return out->status;}
    pid_t child=fork();
    if(child<0){e=-errno;close(p[0]);close(p[1]);out->status=e;return e;}
    if(child==0){
        ix_bar_probe_result r={0};close(p[0]);
        r.status=child_probe(barfd,cfds,fn,ctx,&r);
        child_send(p[1],&r);close(p[1]);_exit(0);
    }
    close(p[1]);
    ix_bar_probe_result result={0};
    size_t got=0;
    while(got<sizeof(result)){
        uint64_t now=ms_now();
        if(!now||now-start>=2000u){e=-ETIMEDOUT;break;}
        struct pollfd waitfd={p[0],POLLIN|POLLHUP,0};
        int n=poll(&waitfd,1,(int)(2000u-(now-start)));
        if(n<0){if(errno==EINTR)continue;e=-errno;break;}
        if(!n){e=-ETIMEDOUT;break;}
        ssize_t bytes=read(p[0],(unsigned char *)&result+got,sizeof(result)-got);
        if(bytes<0){if(errno==EINTR||errno==EAGAIN)continue;e=-errno;break;}
        if(bytes==0){e=-EIO;break;}
        got+=(size_t)bytes;
    }
    close(p[0]);
    pid_t unreaped=0;
    int reaped=reap_bounded(child,e!=0,&unreaped);
    if(!e&&reaped)e=reaped;
    if(!e)*out=result;
    else memset(out,0,sizeof(*out));
    out->unreaped_pid=unreaped;
    out->status=e?e:result.status;
    if(out->status!=0){
        out->raw_temperature=0;
        out->calibration=0;
        out->management_mode=0;
        out->control0=0;
        out->control1=0;
    }
    return out->status;
}
