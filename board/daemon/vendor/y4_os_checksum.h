/* SPDX-License-Identifier: (Apache-2.0 OR GPL-2.0-only) */
#ifndef Y4_OS_CHECKSUM_H
#define Y4_OS_CHECKSUM_H
#include <stddef.h>
#include <stdint.h>
uint32_t y4_crc32c(const void *,size_t);
#endif
