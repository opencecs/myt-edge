#ifndef Y12_POWER_WIRE_H
#define Y12_POWER_WIRE_H

/* Read-only snapshot, indexed by physical slot (front, upper row first).
 * Header: ASCII Y12P, schema byte, slot count, LE16 scan sequence.
 * Entries: flags (bit 0 power valid, bit 1 temperature valid), LE16 mV, signed LE16 mA, signed LE16 tenths of a degree C.
 * Invalid entries carry zero. Host computes mW from mV and mA.
 * Current Y12 hardware reports power only; temperature validity remains clear.
 */
/* Separate read-only identity window, selected as an I2C register. */
#define Y12_IDENTITY_REG 0x80
#define Y12_FW_MAJOR 0
#define Y12_FW_MINOR 1
#define Y12_FW_PATCH 4
#define Y12_POWER_SCHEMA 2
#define Y12_SLOT_COUNT 12
#define Y12_POWER_HEADER_BYTES 8
#define Y12_POWER_ENTRY_BYTES 7
#define Y12_POWER_WIRE_BYTES (Y12_POWER_HEADER_BYTES + Y12_SLOT_COUNT * Y12_POWER_ENTRY_BYTES)

#endif
