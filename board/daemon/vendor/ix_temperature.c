/*
 * ix_temperature.c - IX8024 temperature decoder, pure C11.
 *
 * Pure computation: no I/O, no register access, no MMIO, no sensor
 * initialization, no allocation and no mutable global state. The vendor
 * table below is the only file scope object and it is const.
 */

#include "y4_os_ix_temperature.h"

#include <errno.h>
#include <stddef.h>

/* -ETIME is POSIX; fall back to a valid negative errno if unavailable. */
#ifndef ETIME
#define ETIME ERANGE
#endif

typedef struct {
    int32_t code;
    int32_t millicelsius;
} y4_os_ix_temp_entry;

/*
 * Vendor manual chapter 12.2 / table 43: calibrated code -> temperature.
 * Codes decrease as temperature rises; every entry is an exact table point.
 */
static const y4_os_ix_temp_entry y4_os_ix_temp_table[Y4_OS_IX_TEMP_TABLE_ENTRIES] = {
    {631, -40000}, /* -40 C */
    {623, -35000}, /* -35 C */
    {615, -30000}, /* -30 C */
    {606, -25000}, /* -25 C */
    {598, -20000}, /* -20 C */
    {589, -15000}, /* -15 C */
    {581, -10000}, /* -10 C */
    {572, -5000},  /*  -5 C */
    {564, 0},      /*   0 C */
    {556, 5000},   /*   5 C */
    {547, 10000},  /*  10 C */
    {539, 15000},  /*  15 C */
    {530, 20000},  /*  20 C */
    {522, 25000},  /*  25 C */
    {513, 30000},  /*  30 C */
    {505, 35000},  /*  35 C */
    {496, 40000},  /*  40 C */
    {488, 45000},  /*  45 C */
    {480, 50000},  /*  50 C */
    {471, 55000},  /*  55 C */
    {463, 60000},  /*  60 C */
    {454, 65000},  /*  65 C */
    {446, 70000},  /*  70 C */
    {437, 75000},  /*  75 C */
    {428, 80000},  /*  80 C */
    {420, 85000},  /*  85 C */
    {412, 90000},  /*  90 C */
    {403, 95000},  /*  95 C */
    {394, 100000}, /* 100 C */
    {386, 105000}, /* 105 C */
    {377, 110000}, /* 110 C */
    {369, 115000}, /* 115 C */
    {360, 120000}, /* 120 C */
    {352, 125000}  /* 125 C */
};

/* Clear every field so no stale last-good value can survive a failure. */
static void y4_os_ix_temp_clear(y4_os_ix_temp_reading *reading)
{
    reading->raw_code = 0u;
    reading->calibration_register = 0u;
    reading->calibration = 0;
    reading->calibrated_code = 0;
    reading->millicelsius = 0;
    reading->accuracy_millicelsius = 0;
    reading->sample_time_ns = 0u;
    reading->age_ns = 0u;
    reading->valid = false;
    reading->interpolated = false;
    reading->error = 0;
}

/* Report a failure: cleared output, error recorded, negative errno returned. */
static int y4_os_ix_temp_fail(y4_os_ix_temp_reading *reading, int error)
{
    y4_os_ix_temp_clear(reading);
    reading->error = error;
    return error;
}

/*
 * Signed division rounded to nearest, ties away from zero. The caller
 * guarantees denominator != 0 and small magnitudes (table spans only), so
 * no intermediate can overflow.
 */
static int64_t y4_os_ix_temp_div_round(int64_t numerator, int64_t denominator)
{
    int64_t quotient = numerator / denominator;
    int64_t const remainder = numerator % denominator;

    if (remainder != 0) {
        int64_t const abs_remainder = (remainder < 0) ? -remainder : remainder;
        int64_t const abs_denominator = (denominator < 0) ? -denominator : denominator;

        if ((abs_remainder * 2) >= abs_denominator) {
            quotient += ((numerator < 0) == (denominator < 0)) ? 1 : -1;
        }
    }

    return quotient;
}

/*
 * Map an in-range calibrated code to millidegrees Celsius.
 * Table points are exact; codes between two neighbouring points are linearly
 * interpolated to the nearest millidegree and flagged as approximate.
 */
static bool y4_os_ix_temp_lookup(int32_t code, int32_t *millicelsius, bool *interpolated)
{
    size_t index;

    for (index = 0u; index < Y4_OS_IX_TEMP_TABLE_ENTRIES; ++index) {
        if (y4_os_ix_temp_table[index].code == code) {
            *millicelsius = y4_os_ix_temp_table[index].millicelsius;
            *interpolated = false;
            return true;
        }
    }

    for (index = 0u; (index + 1u) < Y4_OS_IX_TEMP_TABLE_ENTRIES; ++index) {
        int64_t const code_high = (int64_t)y4_os_ix_temp_table[index].code;
        int64_t const code_low = (int64_t)y4_os_ix_temp_table[index + 1u].code;

        if (((int64_t)code < code_high) && ((int64_t)code > code_low)) {
            int64_t const code_span = code_low - code_high; /* negative */
            int64_t const temp_span = (int64_t)y4_os_ix_temp_table[index + 1u].millicelsius -
                                      (int64_t)y4_os_ix_temp_table[index].millicelsius;
            int64_t const offset = (int64_t)code - code_high; /* negative */
            int64_t const fraction = y4_os_ix_temp_div_round(offset * temp_span, code_span);

            *millicelsius =
                (int32_t)((int64_t)y4_os_ix_temp_table[index].millicelsius + fraction);
            *interpolated = true;
            return true;
        }
    }

    return false;
}

int y4_os_ix_temp_decode(uint32_t raw_code,
                         uint32_t calibration_register,
                         uint32_t flags,
                         int read_status,
                         uint64_t sample_time_ns,
                         uint64_t now_ns,
                         uint64_t max_age_ns,
                         y4_os_ix_temp_reading *out)
{
    uint32_t calibration_bits;
    uint32_t calibration_magnitude;
    int64_t calibration;
    int64_t calibrated_code;
    uint64_t age_ns;
    int32_t millicelsius;
    bool interpolated;

    if (out == NULL) {
        return -EINVAL;
    }

    y4_os_ix_temp_clear(out);

    /* The caller must state a positive maximum acceptable age. */
    if (max_age_ns == 0u) {
        return y4_os_ix_temp_fail(out, -EINVAL);
    }

    /* Reserved flag bits are malformed input, never silently ignored. */
    if ((flags & ~Y4_OS_IX_TEMP_FLAG_INITIALIZED) != 0u) {
        return y4_os_ix_temp_fail(out, -EINVAL);
    }

    /* A read status is either success (0) or a negative failure code. */
    if (read_status > 0 || read_status < -4095) {
        return y4_os_ix_temp_fail(out, -EINVAL);
    }

    if (read_status < 0) {
        return y4_os_ix_temp_fail(out, read_status);
    }

    if ((flags & Y4_OS_IX_TEMP_FLAG_INITIALIZED) == 0u) {
        return y4_os_ix_temp_fail(out, -ENODEV);
    }

    if (sample_time_ns == 0u) {
        return y4_os_ix_temp_fail(out, -ERANGE);
    }

    if (sample_time_ns > now_ns) {
        return y4_os_ix_temp_fail(out, -ERANGE);
    }

    age_ns = now_ns - sample_time_ns;
    if (age_ns > max_age_ns) {
        return y4_os_ix_temp_fail(out, -ETIME);
    }

    /* Only the low 7 calibration bits are defined by the vendor manual. */
    calibration_bits = calibration_register & Y4_OS_IX_TEMP_CAL_MASK;
    calibration_magnitude = calibration_bits & Y4_OS_IX_TEMP_CAL_MAGNITUDE_MASK;
    calibration = ((calibration_bits & Y4_OS_IX_TEMP_CAL_SIGN_BIT) != 0u)
                      ? (int64_t)calibration_magnitude
                      : -(int64_t)calibration_magnitude;

    /* Signed 64-bit sum before any range check: no underflow, no overflow. */
    calibrated_code = (int64_t)raw_code + calibration;

    if ((calibrated_code < (int64_t)Y4_OS_IX_TEMP_CODE_MIN) ||
        (calibrated_code > (int64_t)Y4_OS_IX_TEMP_CODE_MAX)) {
        return y4_os_ix_temp_fail(out, -ERANGE);
    }

    if (!y4_os_ix_temp_lookup((int32_t)calibrated_code, &millicelsius, &interpolated)) {
        return y4_os_ix_temp_fail(out, -ERANGE);
    }

    out->raw_code = raw_code;
    out->calibration_register = calibration_register;
    out->calibration = (int32_t)calibration;
    out->calibrated_code = (int32_t)calibrated_code;
    out->millicelsius = millicelsius;
    out->accuracy_millicelsius = Y4_OS_IX_TEMP_VENDOR_ACCURACY_MC;
    out->sample_time_ns = sample_time_ns;
    out->age_ns = age_ns;
    out->valid = true;
    out->interpolated = interpolated;
    out->error = 0;

    return 0;
}
