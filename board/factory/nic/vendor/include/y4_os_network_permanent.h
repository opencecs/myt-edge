#ifndef Y4_OS_NETWORK_PERMANENT_H
#define Y4_OS_NETWORK_PERMANENT_H
/* Readonly driver-reported permanent Ethernet MAC, with repeated index checks.
 * Output unchanged on failure. No current-address fallback or EEPROM write.
 * This is kernel driver evidence, not independent silicon/EEPROM attestation.
 * Caller must bind interface to trusted PCI/board identity and serialize hotplug.
 */
int y4_network_permanent_mac(const char *,unsigned,unsigned char[6]);
#endif
