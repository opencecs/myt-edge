# SPDX-License-Identifier: Apache-2.0
# All targets compile/link only; outputs must stay outside the source checkout.
SOURCE := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
OUT ?= $(abspath $(SOURCE)/../../build/board)
CC ?= cc
DEVICE ?= Y4
SYSROOT ?=
CRYPTO_PREFIX ?=
CFLAGS ?= -O2 -g -std=c11 -Wall -Wextra -Werror -Wshadow -Wformat=2 -Wstrict-prototypes -Wno-misleading-indentation -Wno-format-truncation -ffunction-sections -fdata-sections -fstack-protector-strong -D_FORTIFY_SOURCE=2
SDK_FLAGS :=
SDK_LINK :=
ifneq ($(strip $(SYSROOT)),)
SDK_FLAGS += --sysroot=$(SYSROOT) -isystem $(SYSROOT)/usr/include/aarch64-linux-gnu -isystem $(SYSROOT)/usr/include
SDK_LINK += --sysroot=$(SYSROOT) -L$(SYSROOT)/usr/lib/aarch64-linux-gnu -Wl,-rpath-link,$(SYSROOT)/usr/lib/aarch64-linux-gnu:$(SYSROOT)/lib/aarch64-linux-gnu -Wl,--no-as-needed -l:ld-linux-aarch64.so.1 -Wl,--as-needed
endif
ifneq ($(strip $(CRYPTO_PREFIX)),)
SDK_FLAGS += -I$(CRYPTO_PREFIX)/usr/include -I$(CRYPTO_PREFIX)/usr/include/aarch64-linux-gnu
SDK_LINK += -L$(CRYPTO_PREFIX)/usr/lib/aarch64-linux-gnu -Wl,-rpath-link,$(CRYPTO_PREFIX)/usr/lib/aarch64-linux-gnu
endif
SUB_CFLAGS := $(CFLAGS) $(SDK_FLAGS)
SUB_LINK := $(LDFLAGS) $(SDK_LINK) -Wl,--gc-sections

.PHONY: all daemon network nic mcu-maintain boot-helpers check-grubenv
# The public checkout receives the ARM64 maintainer as a verified binary.
MCU_MAINTAIN_TARGET := $(if $(wildcard $(SOURCE)/factory/mcu/Makefile),mcu-maintain)
all: daemon network nic boot-helpers $(MCU_MAINTAIN_TARGET)
daemon:
	$(MAKE) -C $(SOURCE)/daemon DEVICE="$(DEVICE)" OUT=$(OUT)/daemon CC="$(CC)" SYSROOT="$(SYSROOT)" CFLAGS="$(SUB_CFLAGS)" LDFLAGS="$(SUB_LINK)"
network:
	$(MAKE) -C $(SOURCE)/network DEVICE="$(DEVICE)" OUT=$(OUT)/network CC="$(CC)" SYSROOT="$(SYSROOT)" CFLAGS="$(CFLAGS)"
nic:
	$(MAKE) -C $(SOURCE)/factory/nic DEVICE="$(DEVICE)" OUT=$(OUT)/nic CC="$(CC)" CFLAGS="$(SUB_CFLAGS)" LDFLAGS="$(SUB_LINK)"
mcu-maintain:
	$(MAKE) -C $(SOURCE)/factory/mcu DEVICE="$(DEVICE)" OUT=$(OUT)/mcu-maintain CC="$(CC)" CFLAGS="$(SUB_CFLAGS)" LDFLAGS="$(SUB_LINK)"
boot-helpers: $(OUT)/boot/y4-board-boot-helper
$(OUT)/boot/y4-board-boot-helper: $(SOURCE)/factory/boot_helpers.c $(SOURCE)/display/y4_display.c $(SOURCE)/display/y4_display.h $(SOURCE)/../mcu/include/protocol.h
	mkdir -p $(OUT)/boot
	$(CC) $(SUB_CFLAGS) -I$(SOURCE)/display $(SOURCE)/factory/boot_helpers.c $(SOURCE)/display/y4_display.c $(SUB_LINK) -o $@
check-grubenv: $(OUT)/check-grubenv
$(OUT)/check-grubenv: $(SOURCE)/factory/test_grubenv.c $(SOURCE)/factory/y4_grubenv.c $(SOURCE)/factory/y4_grubenv.h
	mkdir -p $(OUT)
	$(CC) $(SUB_CFLAGS) -I$(SOURCE)/factory $(SOURCE)/factory/test_grubenv.c $(SOURCE)/factory/y4_grubenv.c $(SUB_LINK) -o $@
