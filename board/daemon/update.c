/* SPDX-License-Identifier: Apache-2.0 */
/* GET /api/v1/version: the structured version snapshot (OTA design §6).
 *
 * Sources are the authoritative ones only: the baked release manifest for
 * the slot's build identity, uname for the running kernel, grubenv for the
 * slot state. Fields that cannot be observed are null; nothing is inferred
 * from file names or timestamps. Full firmware fingerprints remain in the
 * hardware snapshot's versions group. */
#define _GNU_SOURCE
#include "board_daemon.h"
#include "y4_grubenv.h"
#include <errno.h>
#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/utsname.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <time.h>

struct text {char *data;size_t cap,used;int error;};
static void put(struct text *t,const char *fmt,...) {
 if(t->error)return;va_list a;va_start(a,fmt);
 int n=vsnprintf(t->data+t->used,t->cap-t->used,fmt,a);va_end(a);
 if(n<0||(size_t)n>=t->cap-t->used)t->error=-EOVERFLOW;else t->used+=(size_t)n;
}
static const char *boolean(int v){return v?"true":"false";}

/* The release manifest is embedded verbatim; the baked kernel value is
 * extracted from that text directly (the generic JSON helper expects
 * compact spacing, json.dumps emits a space after the colon). */
static char release_inner[2048];static int release_have;
static void release_load(void) {
 static int once;
 if(!once){once=1;FILE*f=fopen("/etc/y4-release.json","rb");
  if(f){char raw[2048];size_t n=fread(raw,1,sizeof raw-1,f);fclose(f);
   if(n&&raw[0]=='{'){char*close=strrchr(raw,'}');
    if(close){int m=(int)(close-raw-1);if(m>(int)sizeof release_inner-1)m=(int)sizeof release_inner-1;
     memcpy(release_inner,raw+1,(size_t)m);release_inner[m]=0;release_have=1;}}}}
}
static void release_embed(struct text *t,int *readable) {
 release_load();
 *readable=release_have;
 if(release_have)put(t,"%s,",release_inner);
}
static void release_string(const char *key,char *out,size_t cap) {
 char pattern[64];
 if((size_t)snprintf(pattern,sizeof pattern,"\"%s\": \"",key)>=sizeof pattern)return;
 const char *at=strstr(release_inner,pattern);
 if(!at){(void)snprintf(pattern,sizeof pattern,"\"%s\":\"",key);at=strstr(release_inner,pattern);}
 if(!at)return;
 at+=strlen(pattern);
 const char *end=strchr(at,'"');
 if(!end||(size_t)(end-at)>=cap)return;
 memcpy(out,at,(size_t)(end-at));out[end-at]=0;
}

/* The running slot comes from the kernel command line: root=PARTUUID=<id>
 * resolved through /dev/disk/by-partuuid to the partition name; p2 is slot
 * A and p3 is slot B in the fixed four-partition layout. */
static void running_slot(struct text *t,char *slot) {
 char cmdline[1024]={0};FILE*f=fopen("/proc/cmdline","rb");
 if(f){size_t n=fread(cmdline,1,sizeof cmdline-1,f);fclose(f);cmdline[n]=0;}
 const char *root=strstr(cmdline,"root=PARTUUID=");
 char part[128]={0};
 if(root){
  root+=14;const char*end=root;
  while(*end&&*end!=' '&&*end!='\n'&&end-root<120)end++;
  memcpy(part,root,(size_t)(end-root));
  put(t,"\"partuuid\":\"%s\",",part);
  char link[256],target[256];
  if((size_t)snprintf(link,sizeof link,"/dev/disk/by-partuuid/%s",part)<sizeof link){
   ssize_t n=readlink(link,target,sizeof target-1);
   if(n>0){target[n]=0;char*base=strrchr(target,'/');
    const char*name=base?base+1:target;
    if(strstr(name,"p2")){slot[0]='A';slot[1]=0;}
    else if(strstr(name,"p3")){slot[0]='B';slot[1]=0;}
   }
  }
 } else put(t,"\"partuuid\":null,");
 if(slot[0])put(t,"\"slot\":\"%s\"",slot);else put(t,"\"slot\":null");
}

int y4_board_version_json(const struct y4_board_sample *s,char *out,size_t cap) {
 if(!out||cap<512u)return -EINVAL;
 struct text t={out,cap,0,0};
 int readable=0;
 char running[128]={0};
 struct utsname un={0};
 if(uname(&un)==0)(void)snprintf(running,sizeof running,"%s",un.release);

 struct y4_slot_state state={0};
 int slot_error=y4_grubenv_read(Y4_GRUBENV_DEFAULT,&state);

 release_load();
 char baked_kernel[128]={0};
 release_string("kernel",baked_kernel,sizeof baked_kernel);
 int kernel_sync=baked_kernel[0]&&running[0]&&!strcmp(running,baked_kernel);

 /* Compile-time device family: the binary reports the board it was built
 * for (Y4 today, Y12 builds flip the Makefile DEVICE switch only). */
 put(&t,"{\"schema\":1,\"device\":\"%s\",",Y4_DEVICE_ID);
 if(!strcmp(Y4_DEVICE_ID,"Y12"))
  put(&t,"\"capabilities\":[\"soc-fan-groups\",\"slot-power\"],");
 else
  put(&t,"\"capabilities\":[\"mcu-fans\"],");
 release_embed(&t,&readable);
 put(&t,"\"kernel_running\":");
 if(running[0])put(&t,"\"%s\"",running);else put(&t,"null");
 put(&t,",\"mcu\":");
 /* firmware[0] is the major version: 0.1.38 stores 0 there, so it must
 * not be treated as "no firmware" (same rule as the hardware snapshot). */
int fresh=s&&s->magic==Y4_BOARD_SAMPLE_MAGIC&&!s->mcu_error;
 if(fresh)put(&t,"\"%u.%u.%u\"",s->firmware[0],s->firmware[1],s->firmware[2]);
 else put(&t,"null");

 put(&t,",\"slot\":{\"selected\":");
 if(!slot_error&&state.have_slot)put(&t,"\"%s\"",state.slot);
 else if(!slot_error&&state.have_default)put(&t,"\"%c\"",state.default_entry?'B':'A');
 else put(&t,"null");
 put(&t,",\"boot_ok\":");
 if(!slot_error)put(&t,"%s",boolean(state.boot_ok));else put(&t,"null");
 put(&t,",\"boot_count\":");
 if(!slot_error)put(&t,"%ld",state.boot_count);else put(&t,"null");
 put(&t,",\"boot_count_remaining\":");
 if(!slot_error)put(&t,"%ld",state.boot_count<3?3-state.boot_count:0);
 else put(&t,"null");
 put(&t,",\"boot_kernel\":");
 if(!slot_error&&state.have_boot_kernel)put(&t,"\"%s\"",state.boot_kernel);
 else put(&t,"null");
 put(&t,",\"boot_kernel_a\":");
 if(!slot_error&&state.have_boot_kernel_a)put(&t,"\"%s\"",state.boot_kernel_a);
 else put(&t,"null");
 put(&t,",\"boot_kernel_b\":");
 if(!slot_error&&state.have_boot_kernel_b)put(&t,"\"%s\"",state.boot_kernel_b);
 else put(&t,"null");
 put(&t,",\"running\":{");
 char slot[2]={0};running_slot(&t,slot);
 put(&t,"}},\"consistency\":{\"kernel_sync\":");
 put(&t,"%s",kernel_sync?boolean(1):boolean(0));
 put(&t,",\"slot_confirmed\":");
 if(!slot_error)put(&t,"%s",boolean(state.boot_ok));else put(&t,"null");
 put(&t,",\"manifest_readable\":%s},\"slot_error\":",boolean(readable));
 put(&t,"%d}\n",slot_error);
 if(t.error)return t.error;
 return (int)t.used;
}
/* ---- Slot try-counter, confirmation and rollback (OTA design section 8) ----
 *
 * grub on this board cannot run the counter logic (if/test is unreliable),
 * so the whole protocol lives here and in grubenv - the single mutable
 * version state. The daemon runs it at startup and on its health tick:
 * no extra daemon, no extra units. */
#define Y4_SLOT_CONFIRMED_FILE "/data/y4-board/confirmed.json"
#define Y4_SLOT_TRY_LIMIT 3

static const char *const slot_grubenv_paths[] = {
    Y4_GRUBENV_DEFAULT, Y4_GRUBENV_BACKUP, NULL};

static int slot_write_confirmed(const struct y4_slot_state *state) {
  if (mkdir("/data/y4-board", 0700) && errno != EEXIST) return -errno;
  FILE *f = fopen(Y4_SLOT_CONFIRMED_FILE ".tmp", "w");
  if (!f) return -errno;
  long at = (long)time(NULL);
  if (fprintf(f, "{\"schema\":1,\"default\":%d,\"slot\":\"%s\",\"confirmed_at\":%ld}\n",
              state->default_entry, state->slot[0] ? state->slot : "A", at) < 0) {
    fclose(f); return -EIO;
  }
  if (fclose(f)) return -errno;
  if (rename(Y4_SLOT_CONFIRMED_FILE ".tmp", Y4_SLOT_CONFIRMED_FILE)) return -errno;
  int dir = open("/data/y4-board", O_RDONLY | O_DIRECTORY);
  if (dir >= 0) { (void)fsync(dir); close(dir); }
  return 0;
}

static int slot_read_confirmed(int *default_entry, char *slot, size_t cap) {
  char body[256];
  size_t n = 0;
  int e = y4_board_read_file(Y4_SLOT_CONFIRMED_FILE, body, sizeof body - 1, &n);
  if (e) return e;
  body[n] = 0;
  const char *at = strstr(body, "\"default\":");
  if (!at) return -EINVAL;
  char *endp; long value = strtol(at + 10, &endp, 10);
  if (endp == at + 10 || (value != 0 && value != 1)) return -EINVAL;
  const char *q = strstr(body, "\"slot\":\"");
  if (!q) return -EINVAL;
  q += 8;
  if (!q[0] || q[1] != '"' || (q[0] != 'A' && q[0] != 'B')) return -EINVAL;
  *default_entry = (int)value;
  if (cap) { slot[0] = q[0]; if (cap > 1) slot[1] = 0; }
  return 0;
}

/* Startup duty: an unconfirmed slot counts this try and rolls back to the
 * last confirmed slot after three failures, rebooting before services
 * waste time. A confirmed slot needs nothing here. */
int y4_board_slot_boot_count(void) {
  struct y4_slot_state state;
  int e = y4_grubenv_read(Y4_GRUBENV_DEFAULT, &state);
  if (e == -ENOENT) return 0; /* not provisioned yet; nothing to count */
  if (e) { fprintf(stderr, "y4-board slot: grubenv read failed status=%d\n", e); return e; }
  if (state.boot_ok) return 0;
  state.boot_count += 1;
  fprintf(stderr, "y4-board slot: try boot count=%ld slot=%c\n",
          state.boot_count, state.slot[0] ? state.slot[0] : '?');
  if (state.boot_count < Y4_SLOT_TRY_LIMIT)
    return y4_grubenv_write(slot_grubenv_paths, &state);
  /* Rollback target: the last confirmed slot; without a record, the other
   * slot once - boot_count keeps climbing so an operator still sees it. */
  struct y4_slot_state target = state;
  target.boot_ok = 1;
  target.boot_count = 0;
  int confirmed = 0; char slot[2] = {0};
  e = slot_read_confirmed(&confirmed, slot, sizeof slot);
  if (e == -ENOENT) {
    fprintf(stderr, "y4-board slot: no confirmed record; switching to the other slot\n");
    target.default_entry = state.default_entry ? 0 : 1;
    target.slot[0] = state.default_entry ? 'A' : 'B';
    target.have_default = target.have_slot = 1;
  } else if (e) {
    fprintf(stderr, "y4-board slot: confirmed record unreadable status=%d\n", e);
    return e;
  } else if (confirmed == state.default_entry) {
    fprintf(stderr, "y4-board slot: confirmed slot equals the failing slot\n");
    return 0;
  } else {
    target.default_entry = confirmed;
    target.slot[0] = slot[0];
    target.have_default = target.have_slot = 1;
    fprintf(stderr, "y4-board slot: rolling back to confirmed slot %c\n", slot[0]);
  }
  e = y4_grubenv_write(slot_grubenv_paths, &target);
  if (e) { fprintf(stderr, "y4-board slot: grubenv write failed status=%d\n", e); return e; }
  sync();
  /* Clean reboot first: a forced reboot(2) occasionally wedges this board's
   * boot chain (seen on real hardware with reboot -f and with a direct
   * reboot(2) from this daemon). systemd stops services and syncs
   * filesystems; only if that cannot even be started do we force it. */
  pid_t child = fork();
  if (child == 0) {
    execl("/usr/bin/systemctl", "systemctl", "reboot", (char *)NULL);
    execl("/sbin/reboot", "reboot", (char *)NULL);
    _exit(127);
  }
  if (child > 0) {
    for (int i = 0; i < 100; i++) {          /* up to 10 s */
      struct timespec nap = {0, 100000000};
      (void)nanosleep(&nap, NULL);
      if (kill(child, 0) && errno == ESRCH) break;
    }
    fprintf(stderr, "y4-board slot: clean reboot requested\n");
    return 0;
  }
  fprintf(stderr, "y4-board slot: systemctl unavailable; forcing reboot\n");
  (void)reboot(RB_AUTOBOOT);
  return 0;
}

/* Health-tick duty: once the daemon observes a fresh MCU sample (the same
 * freshness rule the hardware snapshot uses), the slot has demonstrably
 * reached working userspace - confirm it and refresh the confirmed record
 * on the data partition. */
int y4_board_slot_confirm_tick(const struct y4_board_sample *s) {
  static int done;
  if (done || !s || s->magic != Y4_BOARD_SAMPLE_MAGIC || s->mcu_error) return 0;
  done = 1;
  struct y4_slot_state state;
  int e = y4_grubenv_read(Y4_GRUBENV_DEFAULT, &state);
  if (e == -ENOENT) return 0;
  if (e) { fprintf(stderr, "y4-board slot: grubenv read failed status=%d\n", e); return e; }
  if (!state.slot[0]) { state.slot[0] = state.default_entry ? 'B' : 'A'; state.have_slot = 1; }
  if (!state.boot_ok) {
    state.boot_ok = 1;
    state.boot_count = 0;
    e = y4_grubenv_write(slot_grubenv_paths, &state);
    if (e) { fprintf(stderr, "y4-board slot: grubenv write failed status=%d\n", e); return e; }
    fprintf(stderr, "y4-board slot: confirmed slot %c\n", state.slot[0]);
    /* First healthy confirm of this slot: the boot FAT can now drop
     * delivery directories nothing references any more (design 4.3 GC).
     * Best effort; a GC problem must never fail the confirmation. */
    (void)y4_board_kernels_gc();
  }
  return slot_write_confirmed(&state);
}
