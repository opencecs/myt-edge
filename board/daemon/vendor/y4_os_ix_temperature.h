/*
 * y4_os_ix_temperature.h - IX8024 temperature decoder (pure C11).
 *
 * Decodes one raw IX8024 temperature sample together with the sensor
 * calibration register into millidegrees Celsius, following the vendor
 * manual chapter 12.2 / table 43.
 *
 * Scope and non-goals
 * -------------------
 *   - No I/O, no register access, no MMIO, no sensor initialization, no
 *     mutable global state, no allocation. Every input value is supplied by
 *     the caller and the caller owns the output storage.
 *   - The chip-internal addresses named by the vendor manual (raw sample
 *     0x0a001010, calibration 0x0a001c18) are recorded here as documentation
 *     only. This module never touches them; they are not SoC physical MMIO.
 *   - The result is the IX8024 sensor temperature. It is NOT a board
 *     temperature and NOT a SoC temperature, and must not be reported as one.
 *
 * Decoding rules (vendor manual chapter 12.2 / table 43)
 * -----------------------------------------------------
 *   - Only the low 7 bits of the calibration register are defined. Bit 6 is
 *     the sign: when set the low 6 bits are ADDED to the raw code, otherwise
 *     they are SUBTRACTED. Bits 7..31 are ignored.
 *   - The signed sum/difference is formed in 64-bit arithmetic first and is
 *     then range checked. A calibrated code outside 352..631 is rejected;
 *     no clamping, no guessed masking and no extrapolation is performed.
 *   - The vendor table maps calibrated codes to temperatures in 5 C steps.
 *     Table points are exact. Codes between two neighbouring table points
 *     are linearly interpolated to the nearest millidegree and flagged as
 *     approximate via y4_os_ix_temp_reading::interpolated.
 *   - Vendor accuracy is +-5 C, reported as 5000 mC.
 *
 * Validity contract
 * -----------------
 *   - Every failure returns a negative errno value, stores the same value in
 *     y4_os_ix_temp_reading::error, sets valid to false and clears every
 *     value field (including millicelsius), so a stale last-good value is
 *     never exposed.
 *   - A NULL output pointer is invalid and returns -EINVAL; nothing is
 *     written in that case.
 *   - The decoder never reads the output struct, so the caller may pass
 *     storage containing arbitrary bytes.
 *
 * Error codes
 * -----------
 *   -EINVAL  out is NULL, max_age_ns is 0, flags contains a reserved bit, or
 *            read_status is positive or below -4095.
 *   read_status in -4095..-1 is preserved: the sample read failed.
 *   -ENODEV  the initialized flag is not set (sensor not initialized).
 *   -ERANGE  sample_time_ns is 0, sample_time_ns is later than now_ns, or the
 *            calibrated code falls outside 352..631.
 *   -ETIME   the sample is older than the caller supplied maximum age.
 *
 * Check order (first failing check wins): out, max_age_ns, flags, read
 * status, initialized flag, sample_time_ns, now_ns, age, code range.
 */

#ifndef Y4_OS_IX_TEMPERATURE_H
#define Y4_OS_IX_TEMPERATURE_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Caller supplied input flags. Only bit 0 is defined; every other bit is
 * reserved and must be zero, otherwise the call is rejected as malformed.
 */
#define Y4_OS_IX_TEMP_FLAG_INITIALIZED ((uint32_t)0x00000001u)

/* Calibration register layout: only the low 7 bits are defined. */
#define Y4_OS_IX_TEMP_CAL_MASK ((uint32_t)0x7Fu)
#define Y4_OS_IX_TEMP_CAL_SIGN_BIT ((uint32_t)0x40u)
#define Y4_OS_IX_TEMP_CAL_MAGNITUDE_MASK ((uint32_t)0x3Fu)

/* Valid calibrated code window from the vendor table. */
#define Y4_OS_IX_TEMP_CODE_MIN 352
#define Y4_OS_IX_TEMP_CODE_MAX 631

/* Vendor sensor accuracy, +-5 C, expressed in millidegrees Celsius. */
#define Y4_OS_IX_TEMP_VENDOR_ACCURACY_MC 5000

/* Number of vendor table points (631 down to 352, -40 C up to 125 C). */
#define Y4_OS_IX_TEMP_TABLE_ENTRIES 34u

/*
 * Result of one decode attempt.
 *
 * On success: valid is true, error is 0, and every field describes the
 * accepted sample. On failure: valid is false, error holds the negative
 * errno value that was returned, and every other field is 0/false.
 */
typedef struct y4_os_ix_temp_reading {
    uint32_t raw_code;              /* raw sample code as supplied by caller   */
    uint32_t calibration_register;  /* calibration register as supplied        */
    int32_t calibration;            /* signed correction actually applied      */
    int32_t calibrated_code;        /* raw_code + calibration, inside 352..631 */
    int32_t millicelsius;           /* decoded temperature, valid only if valid*/
    int32_t accuracy_millicelsius;  /* vendor accuracy, 5000 mC, on success    */
    uint64_t sample_time_ns;        /* caller supplied sample timestamp        */
    uint64_t age_ns;                /* now_ns - sample_time_ns on success      */
    bool valid;                     /* true only for a fully accepted sample   */
    bool interpolated;              /* true when the value is interpolated     */
    int error;                      /* 0 on success, negative errno otherwise  */
} y4_os_ix_temp_reading;

/*
 * Decode one IX8024 sample.
 *
 *   raw_code              raw temperature code read from the sensor.
 *   calibration_register  raw calibration register value; only the low 7
 *                         bits are used, the upper bits are ignored.
 *   flags                 input flags; set Y4_OS_IX_TEMP_FLAG_INITIALIZED
 *                         when the caller already initialized the sensor.
 *                         This module never initializes hardware. Reserved
 *                         bits are rejected as malformed.
 *   read_status           result of the caller's sample read: 0 for success,
 *                         negative for a read failure. Positive values are
 *                         treated as malformed.
 *   sample_time_ns        timestamp of the sample in nanoseconds, non-zero.
 *   now_ns                current time in nanoseconds, not before the sample.
 *   max_age_ns            caller supplied maximum acceptable sample age in
 *                         nanoseconds; must be positive. A sample whose age
 *                         equals max_age_ns is still accepted.
 *   out                   destination, must not be NULL.
 *
 * Returns 0 on success, or a negative errno value as listed above.
 */
int y4_os_ix_temp_decode(uint32_t raw_code,
                         uint32_t calibration_register,
                         uint32_t flags,
                         int read_status,
                         uint64_t sample_time_ns,
                         uint64_t now_ns,
                         uint64_t max_age_ns,
                         y4_os_ix_temp_reading *out);

#ifdef __cplusplus
}
#endif

#endif /* Y4_OS_IX_TEMPERATURE_H */
