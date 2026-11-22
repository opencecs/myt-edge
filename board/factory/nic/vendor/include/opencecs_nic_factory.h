/* SPDX-License-Identifier: Apache-2.0 */
#ifndef OPENCECS_NIC_FACTORY_H
#define OPENCECS_NIC_FACTORY_H
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>
#define OC_NIC_PORTS 3u
enum oc_nic_role { OC_NIC_LAN = 0, OC_NIC_SFP1 = 1, OC_NIC_SFP2 = 2 };
struct oc_nic_nvm {
  char chip[32];
  uint8_t mac[6];
  int has_mac, gen4, has_speed;
  unsigned write_count, free_bytes;
};
struct oc_nic_plan {
  int write_mac, write_gen4;
};
struct oc_nic_group_result {
  unsigned read_mask, mismatch_mask, attempted_mask, verified_mask;
  unsigned after_valid_mask;
  struct oc_nic_nvm before[3], after[3];
  int status, partial_unknown, cold_reload_required;
};
struct oc_nic_factory_ops {
  void *context;
  int (*read)(void *, unsigned, struct oc_nic_nvm *);
  int (*write)(void *, unsigned, const uint8_t[6], const struct oc_nic_plan *);
  /* Persist the pre-write ambiguity barrier before the first irreversible
   * command. */
  int (*begin)(void *);
  int (*cancelled)(void *);
};
int oc_nic_factory_mac(const uint8_t[12], uint8_t[3][6]);
int oc_nic_factory_parse(const char *, struct oc_nic_nvm *);
int oc_nic_factory_plan(unsigned, const struct oc_nic_nvm *, const uint8_t[6],
                        struct oc_nic_plan *);
int oc_nic_factory_group(const uint8_t[12], int,
                         const struct oc_nic_factory_ops *,
                         struct oc_nic_group_result *);
int oc_nic_factory_profile(unsigned, const uint8_t[6],
                           const struct oc_nic_plan *, char *, size_t);
int oc_nic_factory_write_argv(unsigned, const uint8_t[6],
                              const struct oc_nic_plan *, const char *,
                              const char *, char[13], const char *[12]);
struct oc_nic_child {
  pid_t pid;
  int reaped;
  int wait_status;
  int ownership_error;
};
int oc_nic_factory_run(const char *, const char *const *, const char *, int,
                       unsigned, struct oc_nic_child *);
int oc_nic_factory_reap(struct oc_nic_child *);
/* Debian integration: read-only kill_timeout=1 keeps parent-death kill.
 * Irreversible writes require kill_timeout=0 and an already-held root0600
 * whole-NIC flock FD. The writer inherits a duplicate across exec and has
 * no parent-death kill. A durable pre-fork ambiguous-write barrier is still
 * required: an opaque vendor may close inherited descriptors. Parent death,
 * missing completion or uncertain ownership never authorizes replay. */
int oc_nic_factory_run_guarded(const char *, const char *const *, const char *,
                               int, unsigned, int, int, struct oc_nic_child *);
int oc_nic_factory_run_mode(const char *, const char *const *, const char *,
                            int, unsigned, int, struct oc_nic_child *);
#endif
