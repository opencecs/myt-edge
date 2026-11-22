/* SPDX-License-Identifier: Apache-2.0 */
#include "opencecs_nic_factory.h"
#include <errno.h>
#include <string.h>
#define MASK46 ((UINT64_C(1) << 46) - 1u)
int oc_nic_factory_mac(const uint8_t uid[12], uint8_t mac[3][6]) {
  if (!uid || !mac)
    return -EINVAL;
  unsigned any = 0, notff = 0;
  for (unsigned i = 0; i < 12; i++) {
    any |= uid[i];
    notff |= (unsigned)(uid[i] ^ 255u);
  }
  if (!any || !notff)
    return -ENODATA;
  static const char hex[] = "0123456789abcdef";
  char board[30] = "gd32-";
  for (unsigned i = 0; i < 12; i++) {
    board[5u + i * 2u] = hex[uid[i] >> 4];
    board[6u + i * 2u] = hex[uid[i] & 15u];
  }
  board[29] = 0;
  uint64_t h = UINT64_C(14695981039346656037);
  for (const char *p = board; *p; p++)
    h = (h ^ (unsigned char)*p) * UINT64_C(1099511628211);
  h ^= h >> 33;
  h *= UINT64_C(0xff51afd7ed558ccd);
  h ^= h >> 33;
  h *= UINT64_C(0xc4ceb9fe1a85ec53);
  h ^= h >> 33;
  h &= MASK46;
  if (h > MASK46 - 2u)
    h = MASK46 - 2u;
  for (unsigned i = 0; i < 3; i++) {
    uint64_t v = h + i;
    mac[i][0] = (uint8_t)((((v >> 40) & 63u) << 2) | 2u);
    for (unsigned j = 1; j < 6; j++)
      mac[i][j] = (uint8_t)(v >> (8u * (5u - j)));
  }
  return 0;
}
int oc_nic_factory_plan(unsigned role, const struct oc_nic_nvm *n,
                        const uint8_t desired[6], struct oc_nic_plan *p) {
  if (role >= 3u || !n || !desired || !p)
    return -EINVAL;
  if (strcmp(n->chip, role == OC_NIC_LAN ? "RTL8125BG" : "RTL8127ATF"))
    return -ENODEV;
  if ((desired[0] & 3u) != 2u)
    return -EINVAL;
  *p = (struct oc_nic_plan){
      .write_mac = !n->has_mac || memcmp(n->mac, desired, 6) != 0,
      .write_gen4 = role != OC_NIC_LAN && (!n->has_speed || !n->gen4)};
  if ((p->write_mac || p->write_gen4) && n->free_bytes < 64u)
    return -ENOSPC;
  return 0;
}
int oc_nic_factory_group(const uint8_t uid[12], int reconcile,
                         const struct oc_nic_factory_ops *ops,
                         struct oc_nic_group_result *out) {
  if (!uid || !ops || !ops->read || !ops->write || !ops->begin || !out ||
      (reconcile != 0 && reconcile != 1))
    return -EINVAL;
  struct oc_nic_group_result result = {0};
  struct oc_nic_nvm before[3];
  struct oc_nic_plan plans[3];
  uint8_t desired[3][6];
  int rc = oc_nic_factory_mac(uid, desired);
  for (unsigned i = 0; !rc && i < 3; i++) {
    rc = ops->read(ops->context, i, &before[i]);
    if (!rc) {
      result.read_mask |= 1u << i;
      result.before[i] = before[i];
      rc = oc_nic_factory_plan(i, &before[i], desired[i], &plans[i]);
      if (!rc && (plans[i].write_mac || plans[i].write_gen4))
        result.mismatch_mask |= 1u << i;
    }
  }
  if (!rc && result.mismatch_mask && !reconcile)
    rc = -EUCLEAN;
  if (!rc && result.mismatch_mask && ops->cancelled &&
      ops->cancelled(ops->context))
    rc = -ECANCELED;
  if (!rc && result.mismatch_mask)
    rc = ops->begin(ops->context);
  for (unsigned i = 0; !rc && i < 3; i++)
    if (plans[i].write_mac || plans[i].write_gen4) {
      if (ops->cancelled && ops->cancelled(ops->context)) {
        rc = -ECANCELED;
        break;
      }

      result.attempted_mask |= 1u << i;
      result.cold_reload_required = 1;
      rc = ops->write(ops->context, i, desired[i], &plans[i]);
      if (rc)
        result.partial_unknown = 1;
    }
  /* An unchanged boot is proven by the first complete three-chip snapshot.
   * Any attempted permanent write still requires fresh readback of all chips.
   */
  if (!rc && result.read_mask == 7u && !result.mismatch_mask) {
    result.verified_mask = 7u;
    result.after_valid_mask = 7u;
    memcpy(result.after, result.before, sizeof result.after);
  }
  if (result.read_mask == 7u && result.attempted_mask)
    for (unsigned i = 0; i < 3; i++) {
      struct oc_nic_nvm observed;
      struct oc_nic_plan verify;
      int status = ops->read(ops->context, i, &observed);
      if (!status) {
        result.after[i] = observed;
        result.after_valid_mask |= 1u << i;
        status = oc_nic_factory_plan(i, &observed, desired[i], &verify);
      }
      if (!status && (verify.write_mac || verify.write_gen4))
        status = -EIO;
      if (!status)
        result.verified_mask |= 1u << i;
      else if (!rc)
        rc = status;
    }
  if (result.attempted_mask && result.verified_mask != 7u)
    result.partial_unknown = 1;
  result.status = rc;
  *out = result;
  return rc;
}
