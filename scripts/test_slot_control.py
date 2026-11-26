#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Test A/B slot transitions against isolated GRUB environment files."""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile


HARNESS = r"""
#define _GNU_SOURCE
#include "board_daemon.h"
#include "y4_grubenv.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

static const char parent[]="/data/y4-board";
static const char *const boot_ids[]={
  "ad9cfe48-a7b8-40be-bbf9-2be652612ce0",
  "fdbe403e-983b-49ba-9980-3b421681232d",
  "d051218b-0ce2-4fd5-a89c-ec4aa8efb592",
  "9d3de0b0-9f2b-47a4-bb7f-5d424c752b4f"
};
static unsigned boot, writes, reboots, gc, receipt_attempts;
static int running, ota_busy, backup_missing, primary_missing;
static int reboot_error, receipt_error;
static uint64_t now=1000;
static char receipt[256];
static char primary[512], backup[512], missing[512];

int __real_y4_grubenv_read(const char *, struct y4_slot_state *);
int __real_y4_grubenv_write(const char *const *, const struct y4_slot_state *);

int __wrap_y4_grubenv_read(const char *path, struct y4_slot_state *state) {
  assert(!strcmp(path,Y4_GRUBENV_DEFAULT));
  return __real_y4_grubenv_read(primary,state);
}
int __wrap_y4_grubenv_write(const char *const *paths, const struct y4_slot_state *state) {
  assert(!strcmp(paths[0],Y4_GRUBENV_DEFAULT));
  assert(!strcmp(paths[1],Y4_GRUBENV_BACKUP) && !paths[2]);
  const char *const local[]={primary_missing?missing:primary,backup_missing?missing:backup,NULL};
  int e=__real_y4_grubenv_write(local,state);
  if(!e) writes++;
  return e;
}

int __wrap_y4_board_read_file(const char *path, char *out, size_t cap, size_t *bytes) {
  int n;
  if(!strcmp(path,"/proc/sys/kernel/random/boot_id")) {
    n=snprintf(out,cap,"%s\n",boot_ids[boot]);
  } else {
    assert(!strcmp(path,"/data/y4-board/confirmed.json"));
    if(!receipt[0]) return -ENOENT;
    n=snprintf(out,cap,"%s",receipt);
  }
  assert(n>=0 && (size_t)n<cap);
  if(bytes) *bytes=(size_t)n;
  return 0;
}
int __wrap_y4_board_private_dir(const char *path) {
  assert(!strcmp(path,parent));
  return 0;
}
int __wrap_y4_board_atomic_file(const char *path,const char *name,
                                const void *data,size_t bytes,mode_t mode) {
  assert(!strcmp(path,parent) && !strcmp(name,"confirmed.json"));
  assert(bytes<sizeof receipt && mode==0600);
  receipt_attempts++;
  if(receipt_error) return receipt_error;
  memcpy(receipt,data,bytes);
  receipt[bytes]=0;
  return 0;
}
uint64_t __wrap_y4_board_now_ms(void) { return now; }
int y4_board_running_slot(void) { return running; }
int y4_board_ota_busy(void) { return ota_busy; }
int y4_board_request_reboot(void) { reboots++;return reboot_error; }
int y4_board_kernels_gc(void) { gc++;return 0; }

static struct y4_slot_state read_primary(void) {
  struct y4_slot_state state;
  assert(__real_y4_grubenv_read(primary,&state)==0);
  return state;
}
static void select_slot(int selected, int ok) {
  struct y4_slot_state state={.have_default=1,.default_entry=selected,.have_slot=1,.boot_ok=ok};
  state.slot[0]=(char)('A'+selected);
  const char *const paths[]={primary,backup,NULL};
  assert(__real_y4_grubenv_write(paths,&state)==0);
}
static void check_count(long count) {
  struct y4_slot_state state=read_primary();
  assert(!state.boot_ok && !state.rollback_pending && state.boot_count==count);
  assert(count==0 ? !state.boot_count_id[0] : !strcmp(state.boot_count_id,boot_ids[boot]));
}

int main(int argc,char **argv) {
  assert(argc==2);
  assert(snprintf(primary,sizeof primary,"%s/grubenv",argv[1])<(int)sizeof primary);
  assert(snprintf(backup,sizeof backup,"%s/grubenv-backup",argv[1])<(int)sizeof backup);
  assert(snprintf(missing,sizeof missing,"%s/missing/grubenv",argv[1])<(int)sizeof missing);
  assert(y4_board_slot_pending_reboot()==0);
  select_slot(0,1);
  assert(y4_board_slot_boot_count(0)==0 && !writes);
  assert(y4_board_ota_confirm()==0 && strstr(receipt,"\"slot\":\"A\""));

  select_slot(1,0);
  assert(y4_board_slot_pending_reboot()==1);
  assert(y4_board_ota_confirm()==-EAGAIN);
  assert(y4_board_slot_boot_count(0)==0 && !writes);
  check_count(0);

  running=1;
  assert(y4_board_slot_pending_reboot()==0);
  ota_busy=1;
  assert(y4_board_ota_confirm()==-EBUSY);
  assert(y4_board_slot_boot_count(1)==-EAGAIN && !writes);
  ota_busy=0;
  primary_missing=1;
  assert(y4_board_slot_boot_count(1)==-ENOENT && !writes);
  check_count(0);
  primary_missing=0;
  backup_missing=1;
  assert(y4_board_slot_boot_count(1)==0 && writes==1);
  check_count(1);
  backup_missing=0;
  assert(y4_board_slot_boot_count(1)==0 && writes==1);
  check_count(1);
  struct y4_slot_state untouched_backup;
  assert(__real_y4_grubenv_read(backup,&untouched_backup)==0 && untouched_backup.boot_count==0);

  boot++;
  assert(y4_board_slot_boot_count(1)==0 && writes==2);
  assert(y4_board_slot_boot_count(1)==0 && writes==2);
  check_count(2);
  boot++;
  assert(y4_board_slot_boot_count(0)==-EAGAIN && writes==3 && !reboots);
  assert(y4_board_slot_boot_count(0)==-EAGAIN && writes==3 && !reboots);
  check_count(3);
  assert(y4_board_slot_pending_reboot()==1);

  primary_missing=1;
  assert(y4_board_slot_boot_count(1)==-ENOENT && !reboots && writes==3);
  check_count(3);
  primary_missing=0;
  reboot_error=-EIO;
  assert(y4_board_slot_boot_count(1)==-EIO && reboots==1 && writes==4);
  struct y4_slot_state rollback=read_primary();
  assert(rollback.boot_ok && rollback.rollback_pending && !rollback.boot_count);
  assert(rollback.slot[0]=='A' && !strcmp(rollback.boot_count_id,boot_ids[boot]));
  assert(y4_board_slot_pending_reboot()==1 && y4_board_ota_confirm()==-EAGAIN);
  assert(y4_board_slot_boot_count(0)==-EAGAIN && reboots==1 && writes==4);
  reboot_error=0;
  assert(y4_board_slot_boot_count(1)==0 && reboots==2 && writes==4);
  boot++;
  assert(y4_board_slot_boot_count(1)==0 && reboots==2 && writes==4);
  running=0;
  assert(y4_board_slot_pending_reboot()==0);
  assert(y4_board_slot_boot_count(0)==0 && writes==5);
  assert(!read_primary().rollback_pending);

  select_slot(1,0);
  struct y4_board_sample health={.magic=Y4_BOARD_SAMPLE_MAGIC,.sampled_ms=1000};
  assert(y4_board_slot_confirm_tick(&health)==-EAGAIN && writes==5);
  running=1;
  now=2000;
  health.sampled_ms=1;
  health.mcu_error=-EIO;
  assert(y4_board_slot_confirm_tick(&health)==0 && writes==5);
  health.mcu_error=0;
  now=6000;
  assert(y4_board_slot_confirm_tick(&health)==0 && writes==5);
  health.sampled_ms=now;
  receipt_error=-ENOSPC;
  assert(y4_board_slot_confirm_tick(&health)==-ENOSPC && writes==6);
  assert(read_primary().boot_ok && gc==1);
  assert(strstr(receipt,"\"slot\":\"A\""));
  receipt_error=0;
  assert(y4_board_slot_confirm_tick(&health)==0 && receipt_attempts==2);
  now+=1000;
  assert(y4_board_slot_confirm_tick(&health)==0 && writes==6 && receipt_attempts==3);
  assert(gc==2 && strstr(receipt,"\"slot\":\"B\""));
  now+=1000;
  assert(y4_board_slot_confirm_tick(&health)==0 && receipt_attempts==3);
  assert(y4_board_ota_confirm()==0 && writes==6 && receipt_attempts==4 && gc==3);
  puts("A/B cases passed: roots, confirmation, write failures, boot IDs, and orderly rollback");
  return 0;
}
"""


def main():
    repo = Path(__file__).resolve().parent.parent
    wraps = [
        "y4_grubenv_read", "y4_grubenv_write", "y4_board_read_file",
        "y4_board_private_dir", "y4_board_atomic_file", "y4_board_now_ms",
    ]
    with tempfile.TemporaryDirectory(prefix="myt-test-slot-control-") as tmp:
        root = Path(tmp)
        harness = root / "test_slot_control.c"
        harness.write_text(HARNESS)
        for board in ("Y4", "Y12"):
            output = root / f"test_slot_control-{board}"
            test_dir = root / board
            test_dir.mkdir()
            subprocess.run(
                [
                    *shlex.split(os.getenv("CC", "cc")),
                    "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-misleading-indentation",
                    "-ffunction-sections", "-fdata-sections",
                    f'-DY4_DEVICE_ID="{board}"', "-Iboard/daemon", "-Iboard/factory",
                    "board/daemon/update.c", "board/daemon/util.c", "board/factory/y4_grubenv.c",
                    str(harness), "-Wl,--gc-sections", *[f"-Wl,--wrap={symbol}" for symbol in wraps],
                    "-o", str(output),
                ],
                cwd=repo,
                check=True,
            )
            subprocess.run([str(output), str(test_dir)], check=True)


if __name__ == "__main__":
    main()
