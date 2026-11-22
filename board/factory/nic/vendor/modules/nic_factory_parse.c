/* SPDX-License-Identifier: Apache-2.0 */
#include "opencecs_nic_factory.h"
#include <ctype.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static int unique(const char *text, const char *key, const char **found) {
  *found = strstr(text, key);
  if (!*found)
    return -ENODATA;
  return strstr(*found + strlen(key), key) ? -EBADMSG : 0;
}
static int decimal_field(const char *p, const char *key, unsigned limit,
                         const char *suffix, unsigned *value) {
  p += strlen(key);
  while (*p == ' ' || *p == '\t')
    p++;
  if (!isdigit((unsigned char)*p))
    return -EBADMSG;
  errno = 0;
  char *end;
  unsigned long parsed = strtoul(p, &end, 10);
  if (errno || parsed > limit)
    return -EBADMSG;
  while (*end == ' ' || *end == '\t')
    end++;
  size_t length = strlen(suffix);
  if (strncmp(end, suffix, length))
    return -EBADMSG;
  end += length;
  while (*end == ' ' || *end == '\t')
    end++;
  if (*end && *end != '\r' && *end != '\n')
    return -EBADMSG;
  *value = (unsigned)parsed;
  return 0;
}
static const char *horizontal_space(const char *p) {
  while (*p == ' ' || *p == '\t')
    p++;
  return p;
}
static int line_end(const char *p) {
  p = horizontal_space(p);
  return !*p || *p == '\r' || *p == '\n';
}
static int hex_byte(const char **cursor, unsigned *value) {
  const char *p = *cursor;
  if (!isxdigit((unsigned char)p[0]) || !isxdigit((unsigned char)p[1]))
    return -EBADMSG;
  char pair[3] = {p[0], p[1], 0};
  *value = (unsigned)strtoul(pair, NULL, 16);
  *cursor = p + 2;
  return 0;
}
int oc_nic_factory_parse(const char *text, struct oc_nic_nvm *out) {
  if (!text || !out)
    return -EINVAL;
  memset(out, 0, sizeof(*out));
  if (!strstr(text, "Use EFuse") || strstr(text, "Failed") ||
      strstr(text, "ERROR") || strstr(text, "EEROR"))
    return -EBADMSG;
  const char *p;
  int rc = unique(text, "This is ", &p);
  if (rc)
    return rc;
  if (sscanf(p, "This is %31s", out->chip) != 1)
    return -EBADMSG;
  if (strcmp(out->chip, "RTL8125BG") && strcmp(out->chip, "RTL8127ATF"))
    return -ENODEV;
  rc = unique(text, "Efuse Write Count =", &p);
  if (rc ||
      decimal_field(p, "Efuse Write Count =", 4096, "", &out->write_count))
    return -EBADMSG;
  rc = unique(text, "EFuse Remain ", &p);
  if (rc ||
      decimal_field(p, "EFuse Remain ", 65535, "Bytes!!!", &out->free_bytes))
    return -EBADMSG;
  rc = unique(text, "NODEID =", &p);
  if (!rc) {
    unsigned v[6];
    p = horizontal_space(p + strlen("NODEID ="));
    for (unsigned i = 0; i < 6; i++) {
      if (hex_byte(&p, &v[i]))
        return -EBADMSG;
      if (i < 5 && *p != ' ' && *p != '\t')
        return -EBADMSG;
      p = horizontal_space(p);
    }
    if (!line_end(p))
      return -EBADMSG;
    unsigned zero = 0, ff = 0;
    for (unsigned i = 0; i < 6; i++) {
      out->mac[i] = (uint8_t)v[i];
      zero |= v[i];
      ff |= v[i] ^ 255u;
    }
    if (!zero || !ff) {
      memset(out->mac, 0, sizeof out->mac);
      out->has_mac = 0;
    } else if (out->mac[0] & 1u) {
      return -EBADMSG;
    } else
      out->has_mac = 1;
  } else if (rc != -ENODATA || out->write_count != 0)
    return -EBADMSG;
  rc = unique(text, "PCIELINKSPEED =", &p);
  if (!rc) {
    unsigned speed;
    p = horizontal_space(p + strlen("PCIELINKSPEED ="));
    if (hex_byte(&p, &speed) || speed > 1)
      return -EBADMSG;
    if (!line_end(p)) {
      if (*p != ' ' && *p != '\t')
        return -EBADMSG;
      p = horizontal_space(p);
      const char *annotation = speed ? "(Gen4)" : "(Gen3)";
      if (strncmp(p, annotation, strlen(annotation)))
        return -EBADMSG;
      p += strlen(annotation);
      if (!line_end(p))
        return -EBADMSG;
    }
    out->has_speed = 1;
    out->gen4 = speed == 1;
  } else if (rc != -ENODATA)
    return rc;
  return 0;
}
