#ifndef Y4_OS_MCU_H
#define Y4_OS_MCU_H
#include <stddef.h>
#include <stdint.h>
struct y4_mcu_identity { uint8_t version[3], capabilities; };
/* Borrowed descriptor. Only the owning worker may use the bus concurrently. */
int y4_mcu_read(int fd, uint8_t reg, uint8_t *data, size_t length);
/* Borrowed exclusively owned MCU descriptor. Two readonly UID1/12-byte factory
 * UID reads must match and be nonzero/non-FF. Failure preserves all outputs.
 * Marker/UID are observations, not device authentication. No SSD/random fallback. */
int y4_mcu_factory_uid(int,uint8_t[12]);
/* Opaque board identity domain Y4MC + native UID bytes; not a RFC UUID or proof
 * of physical board authenticity. Replacing MCU requires explicit provisioning. */
int y4_mcu_board_identity(int,uint8_t[16]);
/* Pure shared application/ROM UID domain encoding. Reject zero/FF UID,
 * preserve output on failure; does not authenticate hardware or authorize IO. */
int y4_mcu_board_identity_from_uid(const uint8_t[12],uint8_t[16]);
/* Protocol2 fault bits are not all per-fan stall bits. PWM fault is defined
 * by MCU0.1.23+; reject unknown bits and do not silently clear diagnostics. */
#define Y4_MCU_FAN_FAULT_STALL_MASK 0x0fu
#define Y4_MCU_FAN_FAULT_HOST 0x10u
#define Y4_MCU_FAN_FAULT_PWM 0x20u
static inline unsigned y4_mcu_fan_known_faults(const uint8_t version[3])
{
 return version[0]==0u && version[1]==1u && version[2]>=23u ? 0x3fu : 0x1fu;
}
struct y4_mcu_fans {uint16_t rpm[4];uint8_t effective[4],fault,fan_mask,tach_mask;};
/* Y4M2 only, coherent even sequence snapshot; channel0..3 = FAN3..6.
 * Borrow exclusively owned worker fd; no writes/reset, failed output unchanged. */
int y4_mcu_fans_read(int,const struct y4_mcu_identity *,struct y4_mcu_fans *);
int y12_mcu_identify(int fd, struct y4_mcu_identity *identity, uint8_t uid[12]);
int y4_mcu_identify(int fd, struct y4_mcu_identity *identity);
int y4_mcu_write_verified(int fd, uint8_t reg, uint8_t value);
#endif
