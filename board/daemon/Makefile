# SPDX-License-Identifier: Apache-2.0
# Compilation only. No test, install, service or hardware command is run.
SOURCE := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
BOARD := $(abspath $(SOURCE)/..)
OUT ?= $(abspath $(BOARD)/../../build/daemon)
CC ?= cc
CFLAGS ?= -O2 -g
override CFLAGS += -std=c11 -Wall -Wextra -Werror -Wshadow -Wformat=2 -Wstrict-prototypes -Wno-misleading-indentation -Wno-format-truncation -ffunction-sections -fdata-sections
SYSROOT ?=
ifneq ($(strip $(SYSROOT)),)
PKG_ENV := env PKG_CONFIG_SYSROOT_DIR=$(SYSROOT) PKG_CONFIG_LIBDIR=$(SYSROOT)/usr/lib/aarch64-linux-gnu/pkgconfig:$(SYSROOT)/usr/share/pkgconfig
endif
GIO_CFLAGS := $(shell $(PKG_ENV) pkg-config --cflags gio-2.0)
GIO_LIBS := $(shell $(PKG_ENV) pkg-config --libs gio-2.0)
override CPPFLAGS += -I$(SOURCE) -I$(SOURCE)/vendor -I$(SOURCE)/../factory -I$(BOARD)/network -I$(BOARD)/display $(GIO_CFLAGS)
# Compile-time family in version/hardware/fans/temperature identity ("Y4"/"Y12").
# Product contract: the image build selects DEVICE=Y4 or DEVICE=Y12.
# myt_desktop uses API device to select the matching hardware/API adapter.
# The Y4_DEVICE_ID macro name is historical, not a fixed Y4 return value.
DEVICE ?= Y4
override CPPFLAGS += -DY4_DEVICE_ID=\"$(DEVICE)\"
override LDFLAGS += -Wl,--gc-sections
override LDLIBS += -lcrypto -lzstd -lpam $(GIO_LIBS)
VENDOR := mcu_transport mcu_cooling cooling tmp75_pm mcu_fans mcu_uid mcu_uid_identity mcu_health mcu_backend mcu_backend_transport slots disk_activity crc32c temperature_sample ix_inventory ix_link ix_topology ix_probe ix_bar_collect ix_bar_probe ix_temperature ix_thermal_cache ix_thermal_runtime mcu_ix_display ix_thermal_sampler fan_policy fan1_tach
LOCAL := api resources ssh_api ssh_service ssh_password util status monitor poll_adapter pcie display_adapter nm_state update ota
OBJECTS := $(addprefix $(OUT)/,$(addsuffix .o,$(LOCAL))) $(addprefix $(OUT)/vendor/,$(addsuffix .o,$(VENDOR))) $(OUT)/y4_grubenv.o $(OUT)/y4_network.o $(OUT)/y4_display.o
.PHONY: all
all: $(OUT)/y4-boardd
$(OUT)/y4-boardd: $(OBJECTS)
	$(CC) $(CFLAGS) $(LDFLAGS) $^ $(LDLIBS) -o $@
$(OUT)/%.o: $(SOURCE)/%.c
	@mkdir -p $(dir $@)
	$(CC) $(CPPFLAGS) $(CFLAGS) -MMD -MP -c $< -o $@
$(OUT)/update.o: $(SOURCE)/update.c $(SOURCE)/../factory/y4_grubenv.h
	@mkdir -p $(dir $@)
	$(CC) $(CPPFLAGS) -I$(SOURCE)/../factory $(CFLAGS) -MMD -MP -c $< -o $@
$(OUT)/y4_grubenv.o: $(SOURCE)/../factory/y4_grubenv.c $(SOURCE)/../factory/y4_grubenv.h
	@mkdir -p $(dir $@)
	$(CC) $(CPPFLAGS) -I$(SOURCE)/../factory $(CFLAGS) -MMD -MP -c $< -o $@
$(OUT)/y4_network.o: $(BOARD)/network/y4_network.c
	@mkdir -p $(dir $@)
	$(CC) $(CPPFLAGS) $(CFLAGS) -MMD -MP -c $< -o $@
$(OUT)/y4_display.o: $(BOARD)/display/y4_display.c
	@mkdir -p $(dir $@)
	$(CC) $(CPPFLAGS) $(CFLAGS) -MMD -MP -c $< -o $@
-include $(OBJECTS:.o=.d)
# Vendored OpenCecs sources are pinned by SHA256 in vendor/PROVENANCE.json and
# must not be edited to satisfy host warning flags; relax per object instead.
$(OUT)/vendor/ix_uart_probe.o: CFLAGS += -Wno-format-nonliteral
