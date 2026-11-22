/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include "opencecs_nic_factory.h"
#include "y4_os_mcu_uid_snapshot.h"
#include "y4_os_network_permanent.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <sys/file.h>
#include <ctype.h>
#include <sys/utsname.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <net/if.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#ifndef TOOL
#define TOOL "/usr/libexec/y4-board/rtnicpg"
#endif
#ifndef MODULE
#define MODULE yb_module
static char yb_module[256];
#endif
#ifndef RUNDIR
#define RUNDIR yb_runtime
static char yb_runtime[PATH_MAX]="/run/y4-board/nic/attempt-1";
#endif
#ifndef UIDDIR
#define UIDDIR "/run/y4-hardware"
#endif
static volatile sig_atomic_t stop_requested;
static void request_stop(int signal_number) {
  (void)signal_number;
  stop_requested = 1;
}
static int cancelled(void *opaque) {
  (void)opaque;
  return stop_requested != 0;
}
struct port {
  char bdf[16], bus[8], driver[32];
};
struct context {
  struct port ports[3];
  int dir;
  unsigned serial;
  int child_unknown;
  int lease_fd,restoration_done,restore_status,network_restored;
  unsigned bound_mask,bind_failed_mask;
  struct oc_nic_child child;
};
static uint64_t now_ms(void) {
  struct timespec t;
  if (clock_gettime(CLOCK_MONOTONIC, &t))
    return 0;
  return (uint64_t)t.tv_sec * 1000u + (uint64_t)t.tv_nsec / 1000000u;
}
static void pause_ms(unsigned ms) {
  struct timespec t = {.tv_sec = (time_t)(ms / 1000u),
                       .tv_nsec = (long)(ms % 1000u) * 1000000L};
  while (nanosleep(&t, &t) && errno == EINTR) {
  }
}
static int trusted_file(const char *path, int executable) {
  struct stat s;
  if (lstat(path, &s))
    return -errno;
  return S_ISREG(s.st_mode) && !s.st_uid && !(s.st_mode & 0022) &&
                 (!executable || (s.st_mode & 0111))
             ? 0
             : -EACCES;
}
static int read_text(const char *path, char *out, size_t cap) {
  int fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
  if (fd < 0)
    return -errno;
  ssize_t n = read(fd, out, cap - 1u);
  int rc = n < 0 ? -errno : 0;
  if (n >= 0)
    out[n] = 0;
  if (close(fd) && !rc)
    rc = -errno;
  return rc;
}
/* An explicit terminal MCU failure must not turn into a 31-minute UID wait. */
static int maintenance_failed(int directory) {
  struct stat st;
  if (fstat(directory, &st))
    return -errno;
  if (!S_ISDIR(st.st_mode) || st.st_uid || (st.st_mode & 07777u) != 0700u)
    return -EACCES;
  int fd = openat(directory, "mcu-maintenance-failed",
                  O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
  if (fd < 0)
    return errno == ENOENT ? 0 : -errno;
  int rc = 0;
  if (fstat(fd, &st))
    rc = -errno;
  else if (!S_ISREG(st.st_mode) || st.st_uid ||
           (st.st_mode & 07777u) != 0600u || st.st_nlink != 1u)
    rc = -EACCES;
  else if (st.st_size < 10 || st.st_size > 31)
    rc = -EPROTO;
  char text[33];
  ssize_t n = -1;
  if (!rc) {
    do {
      n = read(fd, text, sizeof text - 1u);
    } while (n < 0 && errno == EINTR);
    if (n < 0)
      rc = -errno;
    else if (n != st.st_size)
      rc = -EPROTO;
  }
  if (close(fd) && !rc)
    rc = -errno;
  if (rc)
    return rc;
  text[n] = 0;
  if (strncmp(text, "status=-", 8u) || text[8] < '1' || text[8] > '9')
    return -EPROTO;
  char *end;
  errno = 0;
  long status = strtol(text + 7, &end, 10);
  if (errno || status >= 0 || status < -4095 || strcmp(end, "\n"))
    return -EPROTO;
  return (int)status;
}

/* LAN, SFP1, SFP2 share physical SoC ports across Y4 and Y12.
 * Interface names are baked in by the same board choice as the kernel. */
static const char *const ancestry[] = {
  "/a0e0000.pcie/", "/a0d0000.pcie/", "/a0c0000.pcie/"
};
#if MYT_NIC_BOARD_Y12
static const char *const names[] = {"y12lan", "y12sfp1", "y12sfp2"};
#else
static const char *const names[] = {"y4lan", "y4sfp1", "y4sfp2"};
#endif

static int discover(struct context *c) {
  unsigned found = 0;
  struct selector {
    unsigned domain, bus, slot, fn;
  } selectors[64];
  unsigned selector_count = 0;
  DIR *d = opendir("/sys/bus/pci/devices");
  if (!d)
    return -errno;
  struct dirent *e;
  int rc = 0;
  while ((e = readdir(d))) {
    unsigned domain, bus, slot, fn;
    char extra;
    if (sscanf(e->d_name, "%x:%x:%x.%x%c", &domain, &bus, &slot, &fn, &extra) !=
            4 ||
        domain > 65535u || bus > 255u || slot > 31u || fn > 7u)
      continue;
    char path[PATH_MAX], real[PATH_MAX], text[64];
    (void)snprintf(path, sizeof path, "/sys/bus/pci/devices/%s", e->d_name);
    if (!realpath(path, real)) {
      rc = -errno;
      break;
    }
    unsigned role;
    for (role = 0; role < 3; role++)
      if (strstr(real, ancestry[role]))
        break;
    (void)snprintf(path, sizeof path, "/sys/bus/pci/devices/%s/vendor",
                   e->d_name);
    if ((rc = read_text(path, text, sizeof text)))
      break;
    if (strtoul(text, NULL, 0) != 0x10ecu)
      continue;
    for (unsigned i = 0; i < selector_count; i++)
      if (selectors[i].bus == bus && selectors[i].slot == slot &&
          selectors[i].fn == fn) {
        rc = -ENOTUNIQ;
        break;
      }
    if (rc)
      break;
    if (selector_count == 64u) {
      rc = -E2BIG;
      break;
    }
    selectors[selector_count++] = (struct selector){domain, bus, slot, fn};
    if (role == 3u)
      continue;
    (void)snprintf(path, sizeof path, "/sys/bus/pci/devices/%s/device",
                   e->d_name);
    if ((rc = read_text(path, text, sizeof text)))
      break;
    if (strtoul(text, NULL, 0) != (role ? 0x8127u : 0x8125u)) {
      rc = -ENODEV;
      break;
    }
    if (found & (1u << role)) {
      rc = -ENOTUNIQ;
      break;
    }
    if (strlen(e->d_name) >= sizeof c->ports[role].bdf) {
      rc = -ENAMETOOLONG;
      break;
    }
    (void)snprintf(path, sizeof path, "/sys/bus/pci/devices/%s/driver",
                   e->d_name);
    if (!realpath(path, real)) {
      rc = -errno;
      break;
    }
    const char *driver = strrchr(real, '/');
    if (!driver || strcmp(driver + 1, role ? "r8127" : "r8125")) {
      rc = -ENODEV;
      break;
    }
    strcpy(c->ports[role].bdf, e->d_name);
    strcpy(c->ports[role].driver, driver + 1);
    (void)snprintf(c->ports[role].bus, sizeof c->ports[role].bus, "%x", bus);
    found |= 1u << role;
  }
  if (closedir(d) && !rc)
    rc = -errno;
  if (!rc && found != 7u)
    rc = -ENODEV;
  for (unsigned i = 0; !rc && i < 3; i++)
    for (unsigned j = 0; j < i; j++)
      if (!strcmp(c->ports[i].bus, c->ports[j].bus))
        rc = -ENOTUNIQ;
  return rc;
}
static int attribute(const struct port *p, const char *verb) {
  char path[256];
  (void)snprintf(path, sizeof path, "/sys/bus/pci/drivers/%s/%s", p->driver,
                 verb);
  int fd = open(path, O_WRONLY | O_NOFOLLOW | O_CLOEXEC);
  if (fd < 0)
    return -errno;
  ssize_t n = write(fd, p->bdf, strlen(p->bdf));
  int rc = n < 0 ? -errno : (size_t)n != strlen(p->bdf) ? -EIO : 0;
  if (close(fd) && !rc)
    rc = -errno;
  return rc;
}
static int execute(struct context *c, unsigned role, const char *const args[],
                   const char *cwd, char raw[65537]) {
  if (strcmp(args[2], "/r") && stop_requested)
    return -ECANCELED;
  char name[80];
  (void)snprintf(name, sizeof name, "port-%u-command-%u.log", role,
                 ++c->serial);
  int fd = openat(c->dir, name,
                  O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
  if (fd < 0)
    return -errno;
  int rc = oc_nic_factory_run_guarded(TOOL, args, cwd ? cwd : RUNDIR, fd,
                                      strcmp(args[2], "/r")==0 ? 15000u : 120000u,
                                      strcmp(args[2], "/r") == 0,
                                      c->lease_fd, &c->child);
  if (c->child.pid > 0 && !c->child.reaped)
    c->child_unknown = 1;
  if (!rc && raw) {
    if (lseek(fd, 0, SEEK_SET) < 0)
      rc = -errno;
    else {
      size_t at = 0;
      while (at < 65537u) {
        ssize_t n = read(fd, raw + at, 65537u - at);
        if (n < 0 && errno == EINTR)
          continue;
        if (n < 0) {
          rc = -errno;
          break;
        }
        if (!n)
          break;
        at += (size_t)n;
      }
      if (at > 65535u)
        rc = -EOVERFLOW;
      else
        raw[at] = 0;
    }
  }
  if (close(fd) && !rc)
    rc = -errno;
  return rc;
}
static int read_nvm(void *opaque, unsigned role, struct oc_nic_nvm *out) {
  struct context *c = opaque;
  if (c->child_unknown)
    return -EBUSY;
  char raw[65537];
  const char *args[] = {TOOL, "/efuse", "/r", "/buspara", c->ports[role].bus,
                        "0",  "0",      NULL};
  int rc = execute(c, role, args, NULL, raw);
  return rc ? rc : oc_nic_factory_parse(raw, out);
}
static int process_birth(unsigned long long *out) {
  char raw[1024];int rc=read_text("/proc/self/stat",raw,sizeof raw);if(rc)return rc;
  char *p=strrchr(raw,')');if(!p||p[1]!=' ')return -EPROTO;p+=2;
  for(unsigned field=3;field<22u;field++){
    if(!*p)return -EPROTO;
    while(*p&&!isspace((unsigned char)*p))p++;
    while(isspace((unsigned char)*p))p++;
  }
  if(!isdigit((unsigned char)*p))return -EPROTO;
  char *end;errno=0;unsigned long long value=strtoull(p,&end,10);
  if(errno||end==p||!value||(*end&&!isspace((unsigned char)*end)))return -EPROTO;
  *out=value;return 0;
}
static int barrier(void *opaque) {
  if (stop_requested)
    return -ECANCELED;
  struct context *c = opaque;
  unsigned long long birth=0;int rc=process_birth(&birth);if(rc)return rc;
  uint64_t started=now_ms();if(!started)return -EIO;
  char marker[512];
  int count=snprintf(marker,sizeof marker,
    "Y4NICWRITE1\nowner_pid=%ld\nowner_birth_ticks=%llu\nstarted_monotonic_ms=%llu\n"
    "Irreversible PG attempt may have started. Missing complete means unknown; "
    "manual recovery is required before any new writer or driver restoration.\n",
    (long)getpid(),birth,(unsigned long long)started);
  if(count<0||(size_t)count>=sizeof marker)return -EOVERFLOW;
  int fd = openat(c->dir, "write-attempted",
                  O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
  if (fd < 0)
    return -errno;
  size_t at=0;
  while(at<(size_t)count){
    ssize_t n=write(fd,marker+at,(size_t)count-at);
    if(n<0&&errno==EINTR)continue;
    if(n<=0){rc=n<0?-errno:-EIO;break;}
    at+=(size_t)n;
  }
  if(!rc&&fsync(fd))rc=-errno;
  if(close(fd)&&!rc)rc=-errno;
  if(!rc&&fsync(c->dir))rc=-errno;
  /* A failed publication deliberately leaves its marker. It never grants
   * permission to replay an attempt whose effects have become ambiguous. */
  return rc;
}
static int write_nvm(void *opaque, unsigned role, const uint8_t mac[6],
                     const struct oc_nic_plan *plan) {
  struct context *c = opaque;
  if (stop_requested)
    return -ECANCELED;
  char hex[13];
  const char *args[12];
  int rc = oc_nic_factory_write_argv(role, mac, plan, TOOL, c->ports[role].bus,
                                     hex, args);
  if (rc)
    return rc;
  if (plan->write_gen4) {
    char directory[PATH_MAX], path[PATH_MAX], cfg[256];
    rc = oc_nic_factory_profile(role, mac, plan, cfg, sizeof cfg);
    if (rc)
      return rc;
    (void)snprintf(directory, sizeof directory, "%s/profile-%u", RUNDIR, role);
    if (mkdir(directory, 0700))
      return -errno;
    int n = snprintf(path, sizeof path, "%s/8127ATFEF.cfg", directory);
    if (n < 0 || (size_t)n >= sizeof path)
      return -ENAMETOOLONG;
    int fd =
        open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0)
      return -errno;
    size_t size = strlen(cfg);
    ssize_t count = write(fd, cfg, size);
    rc = count < 0 ? -errno : (size_t)count == size ? 0 : -EIO;
    if (close(fd) && !rc)
      rc = -errno;
    if (rc)
      return rc;
    return execute(c, role, args, directory, NULL);
  }
  return execute(c, role, args, NULL, NULL);
}

static int verify_live(struct context *c, const uint8_t uid[12]) {
  uint8_t expected[3][6];
  int rc = oc_nic_factory_mac(uid, expected);
  if (rc)
    return rc;
  for (unsigned role = 0; role < 3; role++) {
    char path[PATH_MAX], name[IF_NAMESIZE] = {0}, text[128];
    (void)snprintf(path, sizeof path, "/sys/bus/pci/devices/%s/net",
                   c->ports[role].bdf);
    DIR *dir = opendir(path);
    if (!dir)
      return -errno;
    struct dirent *e;
    unsigned count = 0;
    while ((e = readdir(dir)))
      if (e->d_name[0] != '.') {
        if (strlen(e->d_name) >= sizeof name) {
          rc = -ENAMETOOLONG;
          break;
        }
        strcpy(name, e->d_name);
        count++;
      }
    if (closedir(dir) && !rc)
      rc = -errno;
    if (rc)
      return rc;
    if (count != 1u)
      return -ENOTUNIQ;
    uint8_t permanent[6];
    unsigned index = if_nametoindex(name);
    if (!index)
      return -ENODEV;
    rc = y4_network_permanent_mac(name, index, permanent);
    if (rc)
      return rc;
    if (memcmp(permanent, expected[role], 6))
      return -EAGAIN;
    (void)snprintf(path, sizeof path, "/sys/class/net/%s/address", name);
    rc = read_text(path, text, sizeof text);
    if (rc)
      return rc;
    char expected_text[32];
    (void)snprintf(expected_text, sizeof expected_text,
                   "%02x:%02x:%02x:%02x:%02x:%02x\n", expected[role][0],
                   expected[role][1], expected[role][2], expected[role][3],
                   expected[role][4], expected[role][5]);
    if (strcmp(text, expected_text))
      return -EAGAIN;
    static const char *const properties[] = {
        "current_link_speed", "current_link_width", "max_link_speed",
        "max_link_width"};
    for (unsigned i = 0; i < 4; i++) {
      (void)snprintf(path, sizeof path, "/sys/bus/pci/devices/%s/%s",
                     c->ports[role].bdf, properties[i]);
      int status = read_text(path, text, sizeof text);
      if (!status) {
        text[strcspn(text, "\r\n")] = 0;
        printf("NIC live legacy-role=%u %s=%s\n", role, properties[i], text);
      } else
        printf("NIC live legacy-role=%u %s error=%d\n", role, properties[i],
               status);
    }
  }
  return 0;
}


/* Debian receipt adapter; source group result fields remain distinct. */
static int publish_receipt(struct context *c,
                           const struct y4_mcu_uid_snapshot *uid,
                           const struct oc_nic_group_result *group, int status) {
  char text[3072];size_t at=0;uint8_t expected[3][6];
  int uid_valid=!uid->status&&!oc_nic_factory_mac(uid->uid,expected);
  int n=snprintf(text,sizeof text,
    "{\"schema\":1,\"completed\":%s,\"network_restored\":%s,\"restore_status\":%d,"
    "\"child_pending\":%s,\"status\":%d,\"read_mask\":%u,"
    "\"mismatch_mask\":%u,\"attempted_mask\":%u,\"verified_mask\":%u,"
    "\"after_valid_mask\":%u,\"bound_mask\":%u,\"bind_failed_mask\":%u,"
    "\"partial_unknown\":%s,"
    "\"cold_reload_required\":%s,\"uid_valid\":%s,\"uid\":\"",
    c->restoration_done&&!c->child_unknown?"true":"false",
    c->network_restored?"true":"false",c->restore_status,
    c->child_unknown?"true":"false",status,group->read_mask,group->mismatch_mask,group->attempted_mask,
    group->verified_mask,group->after_valid_mask,c->bound_mask,c->bind_failed_mask,
    group->partial_unknown?"true":"false",group->cold_reload_required?"true":"false",
    uid_valid?"true":"false");
  if(n<0||(size_t)n>=sizeof text)return -EOVERFLOW;at=(size_t)n;
  if(uid_valid)for(unsigned i=0;i<12;i++){n=snprintf(text+at,sizeof text-at,"%02x",uid->uid[i]);if(n<0||(size_t)n>=sizeof text-at)return -EOVERFLOW;at+=(size_t)n;}
  n=snprintf(text+at,sizeof text-at,"\",\"ports\":[");if(n<0||(size_t)n>=sizeof text-at)return -EOVERFLOW;at+=(size_t)n;
  for(unsigned i=0;i<3;i++){
    const struct oc_nic_nvm *before=&group->before[i],*after=&group->after[i];
    n=snprintf(text+at,sizeof text-at,"%s{\"role\":%u,\"before_valid\":%s,\"after_valid\":%s,"
      "\"before_write_count\":%u,\"after_write_count\":%u,\"before_free_bytes\":%u,\"after_free_bytes\":%u}",
      i?",":"",i,(group->read_mask&(1u<<i))?"true":"false",
      (group->after_valid_mask&(1u<<i))?"true":"false",before->write_count,after->write_count,
      before->free_bytes,after->free_bytes);
    if(n<0||(size_t)n>=sizeof text-at)return -EOVERFLOW;at+=(size_t)n;
  }
  n=snprintf(text+at,sizeof text-at,"]}\n");if(n<0||(size_t)n>=sizeof text-at)return -EOVERFLOW;at+=(size_t)n;
  int fd=openat(c->dir,".receipt.tmp",O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
  if(fd<0)return -errno;size_t sent=0;int rc=0;
  while(sent<at){ssize_t put=write(fd,text+sent,at-sent);if(put<0&&errno==EINTR)continue;if(put<=0){rc=put<0?-errno:-EIO;break;}sent+=(size_t)put;}
  if(!rc&&fsync(fd))rc=-errno;if(close(fd)&&!rc)rc=-errno;
  if(!rc&&renameat(c->dir,".receipt.tmp",c->dir,"receipt.json"))rc=-errno;
  if(!rc&&fsync(c->dir))rc=-errno;
  if(rc)(void)unlinkat(c->dir,".receipt.tmp",0);
  return rc;
}
/* Pure kernel/sysfs inspection. Unlike mutating discovery, every physical
 * role is scanned independently, so one missing/unbound NIC cannot suppress
 * later healthy ports. It never runs PG or changes a driver/interface. */
static int inspect_number(const char *raw,unsigned long *value) {
  char *end;errno=0;unsigned long number=strtoul(raw,&end,0);
  if(errno||end==raw)return -EPROTO;
  while(*end&&isspace((unsigned char)*end))end++;
  if(*end)return -EPROTO;*value=number;return 0;
}
static int inspect_ports(void) {
  struct port ports[3]={0};unsigned matches[3]={0};
  int errors[3]={0};unsigned driver_mask=0,usable=0;int summary=0;
  uint8_t macs[3][6]={{0}};
  DIR *pci=opendir("/sys/bus/pci/devices");
  if(!pci)summary=-errno;
  struct dirent *entry;
  while(pci&&(errno=0,entry=readdir(pci))){
    unsigned domain,bus,slot,fn;char extra;
    if(sscanf(entry->d_name,"%x:%x:%x.%x%c",&domain,&bus,&slot,&fn,&extra)!=4||
       domain>65535u||bus>255u||slot>31u||fn>7u)continue;
    char path[PATH_MAX],real[PATH_MAX],raw[64];
    (void)snprintf(path,sizeof path,"/sys/bus/pci/devices/%s",entry->d_name);
    if(!realpath(path,real)){if(!summary)summary=-errno;continue;}
    unsigned role;
    for(role=0;role<3u;role++)if(strstr(real,ancestry[role]))break;
    if(role==3u)continue;
    (void)snprintf(path,sizeof path,"/sys/bus/pci/devices/%s/vendor",entry->d_name);
    int rc=read_text(path,raw,sizeof raw);unsigned long vendor=0;
    if(!rc)rc=inspect_number(raw,&vendor);
    if(rc){if(!errors[role])errors[role]=rc;continue;}
    if(vendor!=0x10ecu)continue;
    if(++matches[role]>1u){errors[role]=-ENOTUNIQ;continue;}
    if(strlen(entry->d_name)>=sizeof ports[role].bdf){errors[role]=-ENAMETOOLONG;continue;}
    (void)snprintf(path,sizeof path,"/sys/bus/pci/devices/%s/device",entry->d_name);
    rc=read_text(path,raw,sizeof raw);unsigned long device=0;
    if(!rc)rc=inspect_number(raw,&device);
    if(!rc&&device!=(role?0x8127u:0x8125u))rc=-ENODEV;
    if(rc){errors[role]=rc;continue;}
    strcpy(ports[role].bdf,entry->d_name);
    (void)snprintf(path,sizeof path,"/sys/bus/pci/devices/%s/driver",entry->d_name);
    if(!realpath(path,real)){errors[role]=-errno;continue;}
    const char *driver=strrchr(real,'/');
    if(!driver||strcmp(driver+1,role?"r8127":"r8125")){errors[role]=-ENODEV;continue;}
    strcpy(ports[role].driver,driver+1);
  }
  if(pci){
    if(errno&&!summary)summary=-errno;
    if(closedir(pci)&&!summary)summary=-errno;
  }
  for(unsigned role=0;role<3u;role++){
    if(!matches[role]&&!errors[role])errors[role]=-ENODEV;
    if(matches[role]>1u)errors[role]=-ENOTUNIQ;
    if(errors[role])continue;
    driver_mask|=1u<<role;
    char path[PATH_MAX],name[IF_NAMESIZE]={0};
    (void)snprintf(path,sizeof path,"/sys/bus/pci/devices/%s/net",ports[role].bdf);
    DIR *net=opendir(path);if(!net){errors[role]=-errno;continue;}
    unsigned count=0;int rc=0;
    while((errno=0,entry=readdir(net))){
      if(entry->d_name[0]=='.')continue;
      if(strlen(entry->d_name)>=sizeof name){rc=-ENAMETOOLONG;break;}
      strcpy(name,entry->d_name);count++;
    }
    if(errno&&!rc)rc=-errno;
    if(closedir(net)&&!rc)rc=-errno;
    if(!rc&&count!=1u)rc=-ENOTUNIQ;
    if(!rc&&strcmp(name,names[role]))rc=-EPROTO;
    unsigned index=rc?0u:if_nametoindex(name);
    if(!rc&&!index)rc=-ENODEV;
    if(!rc)rc=y4_network_permanent_mac(name,index,macs[role]);
    if(!rc){
      unsigned any=0,notff=0;
      for(unsigned j=0;j<6u;j++){any|=macs[role][j];notff|=(unsigned)(macs[role][j]^255u);}
      if(!any||!notff||(macs[role][0]&1u))rc=-EBADMSG;
    }
    errors[role]=rc;if(!rc)usable|=1u<<role;
  }
  unsigned duplicate=0;
  for(unsigned i=0;i<3u;i++)for(unsigned j=i+1u;j<3u;j++)
    if((usable&(1u<<i))&&(usable&(1u<<j))&&!memcmp(macs[i],macs[j],6))
      duplicate|=(1u<<i)|(1u<<j);
  usable&=~duplicate;
  for(unsigned i=0;i<3u;i++){
    if(duplicate&(1u<<i))errors[i]=-EADDRINUSE;
    if(errors[i]&&!summary)summary=errors[i];
  }
  errno=0;int pg_loaded=access("/sys/module/pgdrv",F_OK)==0;
  int pg_status=!pg_loaded&&errno!=ENOENT?-errno:0;
  if(pg_loaded&&!summary)summary=-EBUSY;
  if(pg_status&&!summary)summary=pg_status;
  printf("{\"schema\":1,\"status\":%d,\"driver_mask\":%u,\"usable_mask\":%u,"
         "\"pgdrv_loaded\":%s,\"pgdrv_status\":%d,\"port_status\":[%d,%d,%d]}\n",
         summary,driver_mask,usable,pg_loaded?"true":"false",pg_status,errors[0],errors[1],errors[2]);
  return 0;
}

/* An inherited flock is useful custody, but a closed-source vendor may
 * close it. A pre-fork intent without trusted terminal completion therefore
 * blocks all automatic attempts, even after the previous owner PID exits. */
static int protected_entry(int dir,const char *name) {
  struct stat st;
  if(fstatat(dir,name,&st,AT_SYMLINK_NOFOLLOW))
    return errno==ENOENT?0:-errno;
  return S_ISREG(st.st_mode)&&!st.st_uid&&st.st_nlink==1u&&
         (st.st_mode&07777u)==0600u?1:-EPERM;
}
static int terminal_complete(int dir) {
  int exists=protected_entry(dir,"complete");if(exists<=0)return exists;
  int fd=openat(dir,"complete",O_RDONLY|O_NONBLOCK|O_NOFOLLOW|O_CLOEXEC);
  if(fd<0)return -errno;
  char raw[33];size_t at=0;int rc=0;
  while(at<sizeof raw){
    ssize_t n=read(fd,raw+at,sizeof raw-at);
    if(n<0&&errno==EINTR)continue;
    if(n<0){rc=-errno;break;}
    if(!n)break;
    at+=(size_t)n;
  }
  if(close(fd)&&!rc)rc=-errno;
  if(rc)return rc;
  if(at<9u||at>=sizeof raw||memcmp(raw,"status=",7)||raw[at-1u]!='\n')return -EPROTO;
  raw[at-1u]=0;char *end;errno=0;long value=strtol(raw+7,&end,10);
  if(errno||end==raw+7||*end||value>0||value< -4095)return -EPROTO;
  char canonical[33];int bytes=snprintf(canonical,sizeof canonical,"status=%ld",value);
  return bytes==(int)at-1&&!memcmp(canonical,raw,at)?1:-EPROTO;
}
static int previous_ambiguous(void) {
  int base=open("/run/y4-board/nic",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
  if(base<0)return errno==ENOENT?0:-errno;
  struct stat st;int rc=fstat(base,&st)?-errno:0;
  if(!rc&&(!S_ISDIR(st.st_mode)||st.st_uid||(st.st_mode&07777u)!=0700u))rc=-EPERM;
  for(unsigned i=1;!rc&&i<=5u;i++){
    char name[32];(void)snprintf(name,sizeof name,"attempt-%u",i);
    int dir=openat(base,name,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
    if(dir<0){if(errno!=ENOENT)rc=-errno;continue;}
    if(fstat(dir,&st))rc=-errno;
    else if(!S_ISDIR(st.st_mode)||st.st_uid||(st.st_mode&07777u)!=0700u)rc=-EPERM;
    int marker=rc?rc:protected_entry(dir,"write-attempted");
    if(marker<0)rc=marker;
    else if(marker==1){
      int complete=terminal_complete(dir);
      if(complete!=1)rc=-EOWNERDEAD;
    }
    if(close(dir)&&!rc)rc=-errno;
  }
  if(close(base)&&!rc)rc=-errno;
  return rc;
}

int main(int argc, char **argv) {
  struct sigaction action = {0};
  action.sa_handler = request_stop;
  sigemptyset(&action.sa_mask);
  if (sigaction(SIGTERM, &action, NULL) || sigaction(SIGINT, &action, NULL)) {
    perror("signal handler");
    return 1;
  }
  if(geteuid()){fprintf(stderr,"Root required\n");return 1;}
  if(argc==2&&!strcmp(argv[1],"--inspect"))return inspect_ports();
#if MYT_NIC_BOARD_Y12
  /* Programming on Y12 is permitted only on a matching device tree. */
  FILE *compatible = fopen("/proc/device-tree/compatible", "rb");
  if (!compatible) return 1;
  char compat[512];
  size_t bytes = fread(compat, 1, sizeof compat, compatible);
  int valid = !ferror(compatible) && feof(compatible);
  if (fclose(compatible)) valid = 0;
  static const char expected_compatible[] = "myt,y12";
  int matched = 0;
  for (size_t at = 0; valid && at < bytes;) {
    const char *end = memchr(compat + at, 0, bytes - at);
    if (!end) break;
    size_t size = (size_t)(end - (compat + at));
    if (size == sizeof expected_compatible - 1u && !memcmp(compat + at, expected_compatible, size)) matched = 1;
    at += size + 1u;
  }
  if (!matched) return 1;
#endif
  struct utsname running_kernel;
  if(uname(&running_kernel)){perror("kernel identity");return 1;}
  if(!running_kernel.release[0]||strspn(running_kernel.release,"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+-")!=strlen(running_kernel.release)){
    fprintf(stderr,"Invalid kernel release\\n");return 1;
  }
  int module_bytes=snprintf(yb_module,sizeof yb_module,"/lib/modules/%s/extra/y4/pgdrv.ko",running_kernel.release);
  if(module_bytes<0||(size_t)module_bytes>=sizeof yb_module)return 1;
  unsigned attempt=0;int reconcile=0;
  if(argc==4&&(!strcmp(argv[1],"--reconcile")||!strcmp(argv[1],"--check"))&&!strcmp(argv[2],"--attempt")&&strlen(argv[3])==1&&argv[3][0]>='1'&&argv[3][0]<='5'){
    attempt=(unsigned)(argv[3][0]-'0');reconcile=!strcmp(argv[1],"--reconcile");
  }else{fprintf(stderr,"Usage: y4-nic-factory --inspect | --check|--reconcile --attempt 1..5\n");return 2;}
  (void)snprintf(yb_runtime,sizeof yb_runtime,"/run/y4-board/nic/attempt-%u",attempt);
  int owner=open("/run/y4-board/nic.lock",O_RDWR|O_CREAT|O_NOFOLLOW|O_CLOEXEC,0600);
  struct stat owner_st;
  if(owner<0||fstat(owner,&owner_st)||!S_ISREG(owner_st.st_mode)||owner_st.st_uid||owner_st.st_nlink!=1||(owner_st.st_mode&07777)!=0600||flock(owner,LOCK_EX|LOCK_NB)){
    fprintf(stderr,"NIC ownership unavailable\n");if(owner>=0)close(owner);return 1;
  }
  if (geteuid()) {
    fprintf(stderr, "Root required\n");
    return 1;
  }
  int unresolved=previous_ambiguous();
  if(unresolved){
    fprintf(stderr,"Prior NIC write intent is unresolved: status=%d; manual recovery required\n",unresolved);
    close(owner);return 1;
  }
  struct context c = {.dir = -1,.lease_fd=owner};
  int rc = trusted_file(TOOL, 1);
  if (!rc)
    rc = trusted_file(MODULE, 0);
  if (rc) {
    fprintf(stderr, "Vendor input rejected: %d\n", rc);
    return 1;
  }
  if (mkdir(RUNDIR, 0700) && errno != EEXIST) {
    perror("runtime directory");
    return 1;
  }
  c.dir = open(RUNDIR, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
  struct stat st;
  if (c.dir < 0 || fstat(c.dir, &st) || st.st_uid ||
      (st.st_mode & 07777u) != 0700u) {
    fprintf(stderr, "Runtime directory rejected\n");
    return 1;
  }
  int lock = openat(c.dir, "owner.lock",
                    O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
  if (lock < 0) {
    fprintf(stderr,
            "Already attempted this boot; explicit recovery required\n");
    return 1;
  }
  close(lock);
  struct y4_mcu_uid_snapshot uid = {0};
  uint64_t deadline = now_ms() + 5000u;
  do {
    int fd = open(UIDDIR, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    int terminal = fd < 0 ? 0 : maintenance_failed(fd);
    rc = terminal ? terminal
         : fd < 0 ? -errno
                  : y4_mcu_uid_load(fd, now_ms(), &uid);
    if (!rc && fd >= 0)
      terminal = maintenance_failed(fd);
    if (terminal)
      rc = terminal;
    if (fd >= 0)
      close(fd);
    if (terminal)
      break;
    if (!rc)
      rc = uid.status;
    if (stop_requested) {
      rc = -ECANCELED;
      break;
    }
    if (!rc)
      break;
    pause_ms(100);
  } while (now_ms() < deadline);
  if (!rc && stop_requested)
    rc = -ECANCELED;
  if (!rc) {
    uint8_t expected[3][6];
    rc = oc_nic_factory_mac(uid.uid, expected);
    if (!rc) {
      printf("NIC factory uid=");
      for (unsigned i = 0; i < 12u; i++)
        printf("%02x", uid.uid[i]);
      printf(" algorithm=legacy-fnv1a64-fmix46-v1");
      static const char *const controllers[] = {"a0e0000.pcie", "a0d0000.pcie",
                                                "a0c0000.pcie"};
      for (unsigned i = 0; i < 3u; i++)
        printf(" %s=%02x:%02x:%02x:%02x:%02x:%02x", controllers[i],
               expected[i][0], expected[i][1], expected[i][2], expected[i][3],
               expected[i][4], expected[i][5]);
      putchar('\n');
      fflush(stdout);
    }
  }
  if (!rc)
    rc = discover(&c);
  if (!rc) {
    for (unsigned i = 0; i < 3u; ++i)
      printf("NIC factory legacy-role=%s controller=%s bdf=%s "
             "expected-driver=%s\n",
             i == 0u   ? "lan"
             : i == 1u ? "sfp1"
                       : "sfp2",
             i == 0u   ? "a0e0000.pcie"
             : i == 1u ? "a0d0000.pcie"
                       : "a0c0000.pcie",
             c.ports[i].bdf, c.ports[i].driver);
    fflush(stdout);
  }
  unsigned detached = 0;
  int loaded = 0;
  if (!rc && access("/sys/module/pgdrv", F_OK) == 0)
    rc = -EBUSY;
  while (!rc && detached < 3u) {
    if (stop_requested) {
      rc = -ECANCELED;
      break;
    }
    rc = attribute(&c.ports[detached], "unbind");
    if (!rc)
      detached++;
  }
  if (!rc) {
    int fd = open(MODULE, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0)
      rc = -errno;
    else {
      if (syscall(SYS_finit_module, fd, "", 0))
        rc = -errno;
      else
        loaded = 1;
      close(fd);
    }
  }
  struct oc_nic_group_result result = {0};
  struct oc_nic_factory_ops ops = {&c, read_nvm, write_nvm, barrier, cancelled};
  if (!rc)
    rc = oc_nic_factory_group(uid.uid, reconcile, &ops, &result);
  if (c.child_unknown) {
    int receipt_status =
        publish_receipt(&c, &uid, &result, rc ? rc : -EINPROGRESS);
    if (receipt_status)
      fprintf(stderr, "NIC pending receipt publication failed=%d\n",
              receipt_status);
  }
  if (c.child_unknown) {
    fprintf(stderr,
            "PG child remains owned pid=%ld; networking completion withheld "
            "until actual reap\n",
            (long)c.child.pid);
    fflush(stderr);
    for (;;) {
      int reap = oc_nic_factory_reap(&c.child);
      if (!reap) {
        c.child_unknown = 0;
        break;
      }
      pause_ms(1000);
    }
  }
  int restore = 0;
  int pg_absent = 0;
  if (loaded) {
    if (c.child_unknown)
      restore = -EBUSY;
    else if (syscall(SYS_delete_module, "pgdrv", O_NONBLOCK))
      restore = -errno;
  }
  if (!restore)
    for (unsigned i = 0; i < detached; i++) {
      int x = attribute(&c.ports[i], "bind");
      /* A single port that fails to rebind is isolated in bind_failed_mask;
       * the remaining ports keep their independent restoration outcome. */
      if (x) {
        c.bind_failed_mask |= 1u << i;
        if (!restore)
          restore = x;
      } else {
        c.bound_mask |= 1u << i;
      }
    }
  c.restoration_done=1;
  if(!c.child_unknown){
    errno=0;
    if(access("/sys/module/pgdrv",F_OK)==0)restore=restore?restore:-EBUSY;
    else if(errno!=ENOENT)restore=restore?restore:-errno;
    else pg_absent=1;
  }
  c.network_restored=!restore&&!c.child_unknown&&pg_absent;
  c.restore_status=restore;
  if (!rc)
    rc = restore;
  if (!rc && stop_requested)
    rc = -ECANCELED;
  if (!restore && !c.child_unknown && result.verified_mask == 7u) {
    uint64_t live_deadline = now_ms() + 3000u;
    int live;
    do {
      live = verify_live(&c, uid.uid);
      if (!live)
        break;
      pause_ms(100);
    } while (now_ms() < live_deadline);
    if (live) {
      result.cold_reload_required = 1;
      if (!rc)
        rc = live;
    } else
      result.cold_reload_required = 0;
  }

  printf("{\"status\":%d,\"read_mask\":%u,\"mismatch_mask\":%u,\"attempted_"
         "mask\":"
         "%u,\"verified_mask\":%u,\"bound_mask\":%u,\"bind_failed_mask\":%u,"
         "\"partial_unknown\":%s,\"cold_reload_"
         "required\":"
         "%s,\"restore_status\":%d,\"led_policy\":\"kernel-fiber-polarity\"}\n",
         rc, result.read_mask, result.mismatch_mask, result.attempted_mask,
         result.verified_mask, c.bound_mask, c.bind_failed_mask,
         result.partial_unknown ? "true" : "false",
         result.cold_reload_required ? "true" : "false", restore);
  int receipt_status = publish_receipt(&c, &uid, &result, rc);
  if (receipt_status) {
    fprintf(stderr, "NIC receipt publication failed=%d\n", receipt_status);
    if (!rc)
      rc = receipt_status;
  }
  /* A known terminal restoration publishes its completion even when single
   * ports failed to rebind; only unknown ownership or a still-present PG
   * module leaves the attempt outcome ambiguous. */
  if (pg_absent && !c.child_unknown) {
    int done =
        openat(c.dir, ".complete",
               O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (done >= 0) {
      char text[32];
      int n = snprintf(text, sizeof text, "status=%d\n", rc);
      ssize_t written = write(done, text, (size_t)n);
      int complete = written == n ? 0 : -EIO;
      if(!complete&&fsync(done))complete=-errno;
      if (close(done) && !complete)
        complete = -errno;
      if (!complete && renameat(c.dir, ".complete", c.dir, "complete"))
        complete = -errno;
      if(!complete&&fsync(c.dir))complete=-errno;
      if (complete) {
        fprintf(stderr, "Completion publication failed=%d\n", complete);
        if (!rc)
          rc = complete;
      }
    } else {
      fprintf(stderr, "Completion open failed=%d\n", -errno);
      if (!rc)
        rc = -errno;
    }
  }
  close(c.dir);
  return rc ? 1 : 0;
}
