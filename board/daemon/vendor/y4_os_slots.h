#ifndef Y4_OS_SLOTS_H
#define Y4_OS_SLOTS_H
#include <stdint.h>
struct y4_slots { uint8_t online, type[4]; };
/* PCI enumeration proves presence, not application or storage readiness. */
int y4_slots_read(const char *pci_root, struct y4_slots *slots);
#endif
