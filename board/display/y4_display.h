/* SPDX-License-Identifier: Apache-2.0 */
#ifndef Y4_BOARD_DISPLAY_H
#define Y4_BOARD_DISPLAY_H
#include <stdint.h>
#include "../../mcu/include/protocol.h"
/* Independent wire adapter; no dependency on a network manager or daemon.
 * Physical order LAN,SFP1,SFP2; logical order LAN,Bond. IPv4 is network-byte
 * order. Invalid addresses must be zero. The caller supplies an exclusively
 * owned open MCU I2C fd and keeps /run/y4-board/mcu.lock throughout publish.
 * The fd is never opened, closed or retained by this adapter. */
struct y4_display_input {
    uint8_t physical_valid_mask,physical_present_mask,physical_carrier_mask;
    uint8_t logical_state[2],ipv4_valid_mask,ipv4[2][4],prefix_length[2];
    uint8_t default_egress,initialization_state,attempt,max_attempts;
    int initialization_error,network_error;
    uint16_t sample_age_ms;
    uint8_t initialization_valid;
    uint8_t bond_state,bond_active_mask,bond_lacp_mask;
};
/* Pure encoder: no I/O; errors preserve the caller's output. */
int y4_display_encode(const struct y4_display_input *,uint8_t token,
                      uint8_t frame[Y4_VIEW_FRAME_LEN]);
/* Native I2C_RDWR only. Checks schema, current begin token, per-byte
 * readback and final commit token/status. No exec/sleep/retry or bus reset.
 * Returns -EPROTONOSUPPORT for older firmware, -ESTALE for an accepted
 * frame whose observations are already too old, or another negative errno.
 * A failed/partial publication never extends a previous sample's lifetime. */
int y4_display_publish(int fd,const struct y4_display_input *);
#endif
