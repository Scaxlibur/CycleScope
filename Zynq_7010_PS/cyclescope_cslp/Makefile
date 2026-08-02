SHELL := /bin/bash

HOST_CC ?= cc
WAVEBENCH_PYTHON ?= ../../../tools/wavebench/.venv/bin/python
HOST_CFLAGS := -std=c11 -O2 -g -Wall -Wextra -Werror -pedantic
HOST_BUILD := build/host
HOST_TEST := $(HOST_BUILD)/test_cslp
HOST_SOURCES := \
	src/cslp_protocol.c \
	src/cslp_control.c \
	src/cslp_frame_pool.c \
	tests/test_cslp.c
LAN_PY_TESTS := \
	tests/test_mirror_profile.py \
	tests/test_calibration_profile.py \
	tests/test_adc_analyze.py \
	tests/test_adc_capture.py \
	tests/test_lan_stress.py \
	tests/test_mirror_pcap_compare.py \
	tests/test_pcap_analyze.py \
	tests/test_test_pattern_analyze.py

.PHONY: all test adc-analysis-test vitis vitis-lan-test vitis-mirror clean

all: test vitis

$(HOST_TEST): $(HOST_SOURCES) include/cslp_protocol.h include/cslp_control.h include/cslp_frame_pool.h include/cslp_time.h
	@mkdir -p "$(HOST_BUILD)"
	$(HOST_CC) $(HOST_CFLAGS) -Iinclude $(HOST_SOURCES) -o "$@"

test: $(HOST_TEST)
	@"$(HOST_TEST)"
	@python3 -m unittest $(LAN_PY_TESTS)

adc-analysis-test:
	@test -x "$(WAVEBENCH_PYTHON)"
	@"$(WAVEBENCH_PYTHON)" -m unittest \
		tests/test_adc_analyze.py tests/test_test_pattern_analyze.py

vitis:
	@source /tools/Xilinx/2025.1/Vitis/settings64.sh >/dev/null 2>&1 && \
		vitis -s scripts/build_vitis.py

vitis-lan-test:
	@source /tools/Xilinx/2025.1/Vitis/settings64.sh >/dev/null 2>&1 && \
		CSLP_TEST_PATTERN=1 CSLP_PEER_IPV4_LAST_OCTET=4 \
		vitis -s scripts/build_vitis.py

vitis-mirror:
	@source /tools/Xilinx/2025.1/Vitis/settings64.sh >/dev/null 2>&1 && \
		CSLP_PEER_IPV4_LAST_OCTET=3 CSLP_MIRROR_ENABLED=1 \
		CSLP_MIRROR_IPV4_LAST_OCTET=4 CSLP_MIRROR_UDP_PORT=50002 \
		vitis -s scripts/build_vitis.py

clean:
	rm -rf build
