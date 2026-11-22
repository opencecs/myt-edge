/* SPDX-License-Identifier: Apache-2.0 */
#include "opencecs_nic_factory.h"
#include <errno.h>
#include <stdio.h>
static int valid(unsigned role, const uint8_t mac[6],
                 const struct oc_nic_plan *plan) {
  return role < 3u && mac && plan && (mac[0] & 3u) == 2u &&
         (plan->write_mac == 0 || plan->write_mac == 1) &&
         (plan->write_gen4 == 0 || plan->write_gen4 == 1) &&
         (plan->write_mac || plan->write_gen4) &&
         (!plan->write_gen4 || role != OC_NIC_LAN);
}
int oc_nic_factory_profile(unsigned role, const uint8_t mac[6],
                           const struct oc_nic_plan *plan, char *out,
                           size_t cap) {
  if (!valid(role, mac, plan) || !out || !cap || !plan->write_gen4)
    return -EINVAL;
  int n = plan->write_mac
              ? snprintf(out, cap,
                         "NODEID = %02X %02X %02X %02X %02X "
                         "%02X\nPCIELINKSPEED = 01\nVERSION = 1.033\n",
                         mac[0], mac[1], mac[2], mac[3], mac[4], mac[5])
              : snprintf(out, cap, "PCIELINKSPEED = 01\nVERSION = 1.033\n");
  return n < 0 ? -EIO : (size_t)n >= cap ? -ENOSPC : 0;
}
int oc_nic_factory_write_argv(unsigned role, const uint8_t mac[6],
                              const struct oc_nic_plan *plan, const char *tool,
                              const char *bus, char hex[13],
                              const char *out[12]) {
  if (!valid(role, mac, plan) || !tool || !bus || !hex || !out)
    return -EINVAL;
  const char *p = bus;
  unsigned digits = 0;
  while (*p) {
    if (!((*p >= '0' && *p <= '9') || (*p >= 'a' && *p <= 'f')) ||
        ++digits > 2u)
      return -EINVAL;
    p++;
  }
  if (!digits)
    return -EINVAL;
  for (unsigned i = 0; i < 12; i++)
    out[i] = NULL;
  out[0] = tool;
  out[1] = "/efuse";
  if (plan->write_gen4) {
    out[2] = "/w";
    out[3] = "/buspara";
    out[4] = bus;
    out[5] = "0";
    out[6] = "0";
    out[7] = "/cfgnchg";
    out[8] = "/snnchg";
  } else {
    (void)snprintf(hex, 13, "%02X%02X%02X%02X%02X%02X", mac[0], mac[1], mac[2],
                   mac[3], mac[4], mac[5]);
    out[2] = "/nodeid";
    out[3] = hex;
    out[4] = "/buspara";
    out[5] = bus;
    out[6] = "0";
    out[7] = "0";
    out[8] = "/cfgnchg";
  }
  return 0;
}
