#include "y4_os_mcu.h"
#include <errno.h>
#include <linux/i2c-dev.h>
#include <linux/i2c.h>
#include <string.h>
#include <sys/ioctl.h>
#define MCU_ADDRESS 0x42u
int y4_mcu_read(int fd, uint8_t reg, uint8_t *data, size_t length)
{
    if (fd < 0 || !data || !length || length > 256u - reg) return -EINVAL;
    uint8_t pending[256];
    struct i2c_msg messages[2] = {
        {.addr = MCU_ADDRESS, .flags = 0, .len = 1, .buf = &reg},
        {.addr = MCU_ADDRESS, .flags = I2C_M_RD, .len = (uint16_t)length, .buf = pending}
    };
    struct i2c_rdwr_ioctl_data request = {.msgs = messages, .nmsgs = 2};
    int result = ioctl(fd, I2C_RDWR, &request);
    if (result < 0) return -errno;
    if (result != 2) return -EIO;
    memcpy(data, pending, length);
    return 0;
}
int y4_mcu_identify(int fd, struct y4_mcu_identity *identity)
{
    if (!identity) return -EINVAL;
    uint8_t data[10];
    int result = y4_mcu_read(fd, 0x30, data, sizeof(data));
    if (result) return result;
    if (memcmp(data, "Y4M2", 4) || data[4] != 2 || data[5] != 4) return -EPROTONOSUPPORT;
    struct y4_mcu_identity pending = {{data[6], data[7], data[8]}, data[9]};
    *identity = pending;
    return 0;
}
int y4_mcu_write_verified(int fd, uint8_t reg, uint8_t value)
{
    if (fd < 0) return -EINVAL;
    uint8_t data[2] = {reg, value};
    struct i2c_msg message = {.addr = MCU_ADDRESS, .flags = 0, .len = 2, .buf = data};
    struct i2c_rdwr_ioctl_data request = {.msgs = &message, .nmsgs = 1};
    int result = ioctl(fd, I2C_RDWR, &request);
    if (result < 0) return -errno;
    if (result != 1) return -EIO;
    uint8_t observed;
    result = y4_mcu_read(fd, reg, &observed, 1);
    if (result) return result; /* An error does not prove the write did not happen. */
    return observed == value ? 0 : -EIO;
}
