# SPDX-License-Identifier: Apache-2.0
# Compile-only targets. No services, devices, fixtures or test programs run.
CC ?= cc
AR ?= ar
OUT ?= $(abspath $(CURDIR)/../../../build/network)
SYSROOT ?=
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Werror -Wconversion -Wshadow -pedantic -fstack-protector-strong
DEVICE ?= Y4
CPPFLAGS += -D_FORTIFY_SOURCE=2 -DY4_NET_LAN_NAME=\"$(if $(filter Y12,$(DEVICE)),y12lan,y4lan)\"
ifneq ($(strip $(SYSROOT)),)
SDK_CPPFLAGS := --sysroot=$(SYSROOT) -isystem $(SYSROOT)/usr/include/aarch64-linux-gnu -isystem $(SYSROOT)/usr/include
PKG_ENV := env PKG_CONFIG_SYSROOT_DIR=$(SYSROOT) PKG_CONFIG_LIBDIR=$(SYSROOT)/usr/lib/aarch64-linux-gnu/pkgconfig:$(SYSROOT)/usr/share/pkgconfig
SDK_LDFLAGS := --sysroot=$(SYSROOT) -L$(SYSROOT)/usr/lib/aarch64-linux-gnu -Wl,-rpath-link,$(SYSROOT)/usr/lib/aarch64-linux-gnu:$(SYSROOT)/lib/aarch64-linux-gnu
# The supplied CIX SDK libc.so is a libc.so.6 symlink rather than the Debian
# libc-dev linker script; explicitly retain its existing runtime loader DSO.
SDK_LDLIBS := -Wl,--no-as-needed -l:ld-linux-aarch64.so.1 -Wl,--as-needed
endif
GIO_CFLAGS := $(shell $(PKG_ENV) pkg-config --cflags gio-2.0)
GIO_LIBS := $(shell $(PKG_ENV) pkg-config --libs gio-2.0)

.PHONY: all
all: $(OUT)/y4-network-policy $(OUT)/liby4-network.a

$(OUT):
	mkdir -p "$@"

$(OUT)/y4_network.o: y4_network.c y4_network.h | $(OUT)
	$(CC) $(CPPFLAGS) $(SDK_CPPFLAGS) $(CFLAGS) -c $< -o $@

$(OUT)/y4-network-policy.o: y4-network-policy.c y4_network.h | $(OUT)
	$(CC) $(CPPFLAGS) $(SDK_CPPFLAGS) $(CFLAGS) $(GIO_CFLAGS) -c $< -o $@

$(OUT)/y4-network-policy: $(OUT)/y4-network-policy.o $(OUT)/y4_network.o
	$(CC) $(LDFLAGS) $(SDK_LDFLAGS) $^ -o $@ $(GIO_LIBS) $(SDK_LDLIBS)

$(OUT)/liby4-network.a: $(OUT)/y4_network.o
	$(AR) rcs $@ $<
