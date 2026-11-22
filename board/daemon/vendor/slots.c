#define _XOPEN_SOURCE 700
#include "y4_os_slots.h"
#include <dirent.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static int number(const char *path, const char *attribute, unsigned *value)
{
    char file[PATH_MAX];
    int n = snprintf(file, sizeof(file), "%s/%s", path, attribute);
    if (n < 0 || (size_t)n >= sizeof(file)) return -ENAMETOOLONG;
    FILE *f = fopen(file, "r"); if (!f) return -errno;
    int ok = fscanf(f, "%x", value); fclose(f);
    return ok == 1 ? 0 : -EPROTO;
}
int y4_slots_read(const char *pci_root, struct y4_slots *slots)
{
    if (!pci_root || !slots) return -EINVAL;
    DIR *dir = opendir(pci_root); if (!dir) return -errno;
    struct y4_slots pending = {0};
    struct dirent *entry; int error = 0;
    while ((errno = 0, entry = readdir(dir)) != NULL) {
        if (entry->d_name[0] == '.') continue;
        char path[PATH_MAX], real[PATH_MAX];
        int n = snprintf(path, sizeof(path), "%s/%s", pci_root, entry->d_name);
        if (n < 0 || (size_t)n >= sizeof(path)) { error = -ENAMETOOLONG; break; }
        if (!realpath(path, real)) { error = -errno; break; }
        /* The IX8024 downstream hotplug ports sit at bus c2, function 00/03/06/09.
         * Match the ":bus:dev.fn/" suffix without the PCI domain: the domain
         * number depends on controller probe order (the switch enumerated on
         * 0003 on this board while the original handoff assumed 0004), while
         * bus c2 exists only under the switch on every layout. */
        const char *ports[] = {":c2:00.0/", ":c2:03.0/", ":c2:06.0/", ":c2:09.0/"};
        int slot = -1;
        for (unsigned i = 0; i < 4; ++i) if (strstr(real, ports[i])) slot = (int)i;
        if (slot < 0) continue;
        unsigned cls, vendor, device;
        if ((error = number(path, "class", &cls)) != 0) break;
        if ((cls >> 16) == 6) continue;
        if ((error = number(path, "vendor", &vendor)) != 0 ||
            (error = number(path, "device", &device)) != 0) break;
        uint8_t type = cls == 0x010802 ? 2 : 1;
        if (vendor == 0x1d87 && device == 0x182a) type = 3;
        if ((vendor == 0x1f6b || vendor == 0x1ec8) && device == 0x0c00) type = 4;
        /* Multiple functions with different types are shown as generic PCIe. */
        if (pending.type[slot] && pending.type[slot] != type) type = 1;
        pending.type[slot] = type;
        pending.online |= (uint8_t)(1u << (unsigned)slot);
    }
    if (!error && errno) error = -errno;
    closedir(dir);
    if (!error) *slots = pending;
    return error;
}
