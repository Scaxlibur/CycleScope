SHELL := /bin/bash

HOST_CC ?= cc
HOST_CFLAGS := -std=c11 -O2 -g -Wall -Wextra -Werror -pedantic
HOST_BUILD := build/host
HOST_TEST := $(HOST_BUILD)/test_cslp
HOST_SOURCES := \
	src/cslp_protocol.c \
	src/cslp_control.c \
	src/cslp_frame_pool.c \
	tests/test_cslp.c

.PHONY: all test vitis clean

all: test vitis

$(HOST_TEST): $(HOST_SOURCES) include/cslp_protocol.h include/cslp_control.h include/cslp_frame_pool.h
	@mkdir -p "$(HOST_BUILD)"
	$(HOST_CC) $(HOST_CFLAGS) -Iinclude $(HOST_SOURCES) -o "$@"

test: $(HOST_TEST)
	@"$(HOST_TEST)"

vitis:
	@source /tools/Xilinx/2025.1/Vitis/settings64.sh >/dev/null 2>&1 && \
		vitis -s scripts/build_vitis.py

clean:
	rm -rf build
