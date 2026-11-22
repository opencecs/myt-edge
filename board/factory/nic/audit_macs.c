/* SPDX-License-Identifier: Apache-2.0 */
#define _POSIX_C_SOURCE 200809L
#include "opencecs_nic_factory.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct mac_entry {
  uint8_t mac[6];
  size_t line;
  unsigned port;
};

static int hex_digit(char c) {
  if (c >= '0' && c <= '9')
    return c - '0';
  if (c >= 'a' && c <= 'f')
    return c - 'a' + 10;
  if (c >= 'A' && c <= 'F')
    return c - 'A' + 10;
  return -1;
}

static int parse_uid(const char *text, uint8_t uid[12]) {
  for (size_t i = 0; i < 12; i++) {
    int hi = hex_digit(text[2 * i]);
    int lo = hex_digit(text[2 * i + 1]);
    if (hi < 0 || lo < 0)
      return -1;
    uid[i] = (uint8_t)((hi << 4) | lo);
  }
  return 0;
}

static int by_mac(const void *a, const void *b) {
  const struct mac_entry *first = a, *second = b;
  return memcmp(first->mac, second->mac, sizeof first->mac);
}

int main(void) {
  static const char *const ports[] = {"lan", "sfp1", "sfp2"};
  char *text = NULL;
  size_t text_size = 0, line = 0, count = 0, capacity = 0;
  struct mac_entry *entries = NULL;
  int status = 1;
  ssize_t len;

  while ((len = getline(&text, &text_size, stdin)) >= 0) {
    uint8_t uid[12], mac[3][6];
    line++;
    while (len > 0 && (text[len - 1] == '\n' || text[len - 1] == '\r'))
      len--;
    if (len != 24 || parse_uid(text, uid) || oc_nic_factory_mac(uid, mac)) {
      fprintf(stderr, "Invalid MCU UID on line %zu\n", line);
      goto done;
    }
    if (capacity - count < 3) {
      if (capacity > SIZE_MAX / sizeof *entries / 2)
        goto done;
      size_t next = capacity ? capacity * 2 : 192;
      struct mac_entry *grown = realloc(entries, next * sizeof *entries);
      if (!grown) {
        perror("allocate addresses");
        goto done;
      }
      entries = grown;
      capacity = next;
    }
    for (unsigned port = 0; port < 3; port++) {
      memcpy(entries[count].mac, mac[port], sizeof mac[port]);
      entries[count].line = line;
      entries[count].port = port;
      count++;
    }
  }
  if (!feof(stdin) || !count) {
    fprintf(stderr, "Supply one 24-digit hexadecimal MCU UID per line\n");
    goto done;
  }
  qsort(entries, count, sizeof *entries, by_mac);
  status = 0;
  for (size_t i = 1; i < count; i++) {
    const struct mac_entry *a = &entries[i - 1], *b = &entries[i];
    if (by_mac(a, b) == 0) {
      fprintf(stderr, "MAC conflict: input line %zu %s and line %zu %s\n",
              a->line, ports[a->port], b->line, ports[b->port]);
      status = 1;
    }
  }
  if (!status)
    printf("Verified %zu addresses from %zu MCU UIDs in this inventory\n",
           count, count / 3);
done:
  free(entries);
  free(text);
  return status;
}
