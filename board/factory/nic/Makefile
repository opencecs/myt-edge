# SPDX-License-Identifier: Apache-2.0
CC ?= cc
OUT ?= $(abspath $(CURDIR)/../../../../build/nic)
DEVICE ?= Y4
CPPFLAGS += -Ivendor/include -DMYT_NIC_BOARD_Y12=$(if $(filter Y12,$(DEVICE)),1,0)
CFLAGS ?= -O2 -std=c11 -Wall -Wextra -Werror -Wno-misleading-indentation -Wno-format-truncation
SOURCES := y4_nic_factory.c $(wildcard vendor/modules/*.c) vendor/core/crc32c.c
.PHONY: all
all: $(OUT)/y4-nic-factory
$(OUT)/y4-nic-factory: $(SOURCES) $(wildcard vendor/include/*.h)
	mkdir -p $(OUT)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(SOURCES) $(LDFLAGS) -o $@

.PHONY: audit-macs
audit-macs: $(OUT)/audit-macs
$(OUT)/audit-macs: audit_macs.c vendor/modules/nic_factory.c vendor/include/opencecs_nic_factory.h
	mkdir -p $(OUT)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(filter %.c,$^) $(LDFLAGS) -o $@
