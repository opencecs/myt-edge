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
  if(f){char raw[2048];size_t n=fread(raw,1,sizeof raw-1,f);fclose(f);raw[n]=0;
   if(n&&raw[0]=='{'){char*close=strrchr(raw,'}');
    if(close){int m=(int)(close-raw-1);if(m>(int)sizeof release_inner-1)m=(int)sizeof release_inner-1;
     memcpy(release_inner,raw+1,(size_t)m);release_inner[m]=0;release_have=1;}}}}
}
static void release_embed(struct text *t,int *readable) {
 release_load();
 *readable=release_have;
 if(release_have&&release_inner[strspn(release_inner," \t\r\n")])put(t,"%s,",release_inner);
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

static int ota_slot_ready(const struct y4_slot_state *state);

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
 if(!slot_error)put(&t,"%s",boolean(!ota_slot_ready(&state)));else put(&t,"null");
 put(&t,",\"manifest_readable\":%s},\"slot_error\":",boolean(readable));
 put(&t,"%d}\n",slot_error);
 if(t.error)return t.error;
 return (int)t.used;
}
/* ---- Slot try-counter, confirmation and rollback (OTA design section 8) ---- */
#define Y4_SLOT_STATE_DIR "/data/y4-board"
#define Y4_SLOT_CONFIRMED_FILE Y4_SLOT_STATE_DIR "/confirmed.json"
#define Y4_SLOT_TRY_LIMIT 3
#define Y4_SLOT_CONFIRM_RETRY_MS 1000u
#define Y4_SLOT_HEALTH_MAX_AGE_MS 5000u

static const char *const slot_grubenv_paths[] = {
    Y4_GRUBENV_DEFAULT, Y4_GRUBENV_BACKUP, NULL};

static int selected_slot(const struct y4_slot_state *state) {
  if (!state->have_default || (state->default_entry != 0 && state->default_entry != 1))
    return -EINVAL;
  if (state->have_slot && state->slot[0] != 'A' + state->default_entry)
    return -EINVAL;
  return state->default_entry;
}

static int current_boot_id(char id[37]) {
  char text[40];
  size_t bytes = 0;
  int e = y4_board_read_file("/proc/sys/kernel/random/boot_id", text, sizeof text, &bytes);
  if (e) return e;
  if (bytes && text[bytes - 1] == '\n') text[--bytes] = 0;
  if (!y4_boot_id_valid(text)) return -EPROTO;
  memcpy(id, text, 37);
  return 0;
}

static int slot_write_confirmed(int selected) {
  int e = y4_board_private_dir(Y4_SLOT_STATE_DIR);
  if (e) return e;
  char text[128];
  int n = snprintf(text, sizeof text,
                   "{\"schema\":1,\"default\":%d,\"slot\":\"%c\",\"confirmed_at\":%ld}\n",
                   selected, 'A' + selected, (long)time(NULL));
  if (n < 0 || (size_t)n >= sizeof text) return -EOVERFLOW;
  return y4_board_atomic_file(Y4_SLOT_STATE_DIR, "confirmed.json", text, (size_t)n, 0600);
}

static int slot_read_confirmed(void) {
  char text[256], slot[2];
  long long schema = 0, selected = -1;
  int e = y4_board_read_file(Y4_SLOT_CONFIRMED_FILE, text, sizeof text, NULL);
  if (e) return e;
  if (y4_board_json_number(text, "schema", &schema) || schema != 1 ||
      y4_board_json_number(text, "default", &selected) || (selected != 0 && selected != 1) ||
      y4_board_json_string(text, "slot", slot, sizeof slot) || slot[0] != 'A' + selected)
    return -EINVAL;
  return (int)selected;
}

/* The other slot is the rollback target until the running slot has both
 * confirmation records. A failed confirmation can leave boot_ok set while
 * confirmed.json still names the only known-good fallback. */
static int ota_slot_ready(const struct y4_slot_state *state) {
  int selected = selected_slot(state);
  if (selected < 0) return selected;
  int running = y4_board_running_slot();
  if (running < 0) return running;
  if (state->rollback_pending || selected != running ||
      (!state->boot_ok && state->boot_count >= Y4_SLOT_TRY_LIMIT)) return -EBUSY;
  if (!state->boot_ok) return -EAGAIN;
  int confirmed = slot_read_confirmed();
  if (confirmed == -ENOENT) return -EAGAIN;
  if (confirmed < 0) return confirmed;
  return confirmed == running ? 0 : -EAGAIN;
}

int y4_board_ota_slot_ready(void) {
  struct y4_slot_state state;
  int e = y4_grubenv_read(Y4_GRUBENV_DEFAULT, &state);
  return e ? e : ota_slot_ready(&state);
}

int y4_board_slot_pending_reboot(void) {
  struct y4_slot_state state;
  int e = y4_grubenv_read(Y4_GRUBENV_DEFAULT, &state);
  if (e == -ENOENT) return 0;
  if (e) return e;
  if (state.boot_ok && !state.rollback_pending) return 0;
  int selected = selected_slot(&state);
  if (selected < 0) return selected;
  int running = y4_board_running_slot();
  if (running < 0) return running;
  return running != selected || (!state.boot_ok && state.boot_count >= Y4_SLOT_TRY_LIMIT);
}

int y4_board_slot_boot_count(int allow_rollback) {
  if (y4_board_ota_busy()) return -EAGAIN;
  struct y4_slot_state state;
  int e = y4_grubenv_read(Y4_GRUBENV_DEFAULT, &state);
  if (e == -ENOENT) return 0;
  if (e) return e;
  if (state.boot_ok && !state.rollback_pending) return 0;
  int selected = selected_slot(&state);
  if (selected < 0) return selected;
  int running = y4_board_running_slot();
  if (running < 0) return running;
  if (running != selected && !state.rollback_pending) return 0;

  char id[37];
  e = current_boot_id(id);
  if (e) return e;
  if (state.rollback_pending) {
    if (running == selected) {
      state.rollback_pending = 0;
      state.boot_count_id[0] = 0;
      return y4_grubenv_write(slot_grubenv_paths, &state);
    }
    /* Requeue if the service restarted or D-Bus failed before the reboot.
     * A manually selected slot in a later boot does not trigger a loop. */
    if (strcmp(id, state.boot_count_id)) return 0;
    return allow_rollback ? y4_board_request_reboot() : -EAGAIN;
  }

  if (strcmp(id, state.boot_count_id)) {
    if (state.boot_count < Y4_SLOT_TRY_LIMIT) state.boot_count++;
    memcpy(state.boot_count_id, id, sizeof id);
    e = y4_grubenv_write(slot_grubenv_paths, &state);
    if (e) return e;
    fprintf(stderr, "y4-board slot: try boot count=%ld slot=%c\n",
            state.boot_count, 'A' + running);
  }
  if (state.boot_count < Y4_SLOT_TRY_LIMIT) return 0;
  if (!allow_rollback) return -EAGAIN;

  int confirmed = slot_read_confirmed();
  if (confirmed == -ENOENT) {
    confirmed = 1 - selected;
    fprintf(stderr, "y4-board slot: no confirmed record; switching to the other slot\n");
  } else if (confirmed < 0) return confirmed;
  else if (confirmed == selected) return -EUCLEAN;

  state.default_entry = confirmed;
  state.have_slot = 1;
  state.slot[0] = (char)('A' + confirmed);
  state.boot_ok = 1;
  state.boot_count = 0;
  state.rollback_pending = 1;
  /* Persist the reboot intent with the new default. Once the systemd job
   * is queued, it can safely be monitored across a daemon restart. */
  e = y4_grubenv_write(slot_grubenv_paths, &state);
  if (e) return e;
  fprintf(stderr, "y4-board slot: rolling back to slot %c\n", state.slot[0]);
  return y4_board_request_reboot();
}

int y4_board_ota_confirm(void) {
  if (y4_board_ota_busy()) return -EBUSY;
  struct y4_slot_state state;
  int e = y4_grubenv_read(Y4_GRUBENV_DEFAULT, &state);
  if (e) return e;
  int selected = selected_slot(&state);
  if (selected < 0) return selected;
  int running = y4_board_running_slot();
  if (running < 0) return running;
  if (selected != running) return -EAGAIN;

  if (!state.boot_ok || !state.have_slot || state.boot_count || state.rollback_pending ||
      state.boot_count_id[0]) {
    state.have_slot = 1;
    state.slot[0] = (char)('A' + running);
    state.boot_ok = 1;
    state.boot_count = 0;
    state.boot_count_id[0] = 0;
    state.rollback_pending = 0;
    e = y4_grubenv_write(slot_grubenv_paths, &state);
    if (e) return e;
    fprintf(stderr, "y4-board slot: confirmed slot %c\n", state.slot[0]);
  }
  e = slot_write_confirmed(running);
  if (e) return e;
  (void)y4_board_kernels_gc();
  return 0;
}

/* Attempt confirmation once fresh MCU health is visible. A transient
 * grubenv, mount, or receipt error remains retryable in this process. */
int y4_board_slot_confirm_tick(const struct y4_board_sample *s) {
  static int done;
  static uint64_t retry_at;
  if (done || y4_board_ota_busy() || !s || s->magic != Y4_BOARD_SAMPLE_MAGIC ||
      s->mcu_error || !s->sampled_ms) return 0;
  uint64_t now = y4_board_now_ms();
  if (now < s->sampled_ms || now - s->sampled_ms >= Y4_SLOT_HEALTH_MAX_AGE_MS ||
      now < retry_at) return 0;
  int e = y4_board_ota_confirm();
  if (!e) done = 1;
  else {
    retry_at = now + Y4_SLOT_CONFIRM_RETRY_MS;
    if (e != -EAGAIN && e != -ENOENT)
      fprintf(stderr, "y4-board slot: automatic confirmation deferred status=%d\n", e);
  }
  return e;
}
