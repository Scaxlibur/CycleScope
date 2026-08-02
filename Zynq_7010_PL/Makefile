SHELL := /bin/bash
ROOT := $(abspath ..)
ENV := source "$(CURDIR)/scripts/xilinx_env.sh"
WAVEBENCH_PYTHON ?= $(ROOT)/tools/wavebench/.venv/bin/python
RAW_IOB_ILA_PHASE ?= 300
RAW_IOB_ILA_BUILD := build/diagnostic/raw-iob-ila-p$(RAW_IOB_ILA_PHASE)
RAW_IOB_ILA_STEM := cyclescope_system_raw_iob_ila_p$(RAW_IOB_ILA_PHASE)

.PHONY: version sim synth synth-filter synth-core system bitstream \
		raw-iob-ila raw-iob-ila-0 raw-iob-ila-30 raw-iob-ila-60 \
		raw-iob-ila-90 raw-iob-ila-120 raw-iob-ila-150 raw-iob-ila-180 \
		raw-iob-ila-210 raw-iob-ila-240 raw-iob-ila-270 raw-iob-ila-300 raw-iob-ila-330 \
		raw-iob-ila-345 raw-iob-ila-348 raw-iob-ila-351 raw-iob-ila-354 \
		raw-iob-analysis-test clean

version:
	@$(ENV) && vivado -version | sed -n '1,5p' && v++ --version 2>&1 | sed -n '1,7p'

sim:
	@$(ENV) && vivado -mode batch -nolog -nojournal -source scripts/run_sim.tcl

synth: synth-filter synth-core

synth-filter:
	@$(ENV) && vivado -mode batch -nolog -nojournal -source scripts/run_synth.tcl

synth-core:
	@$(ENV) && vivado -mode batch -nolog -nojournal -source scripts/run_core_synth.tcl

system:
	@$(ENV) && vivado -mode batch -nolog -nojournal -source scripts/build_system.tcl

bitstream:
	@$(ENV) && vivado -mode batch -nolog -nojournal \
		-source scripts/build_system.tcl -tclargs --implement
	@unzip -Z1 build/system/hardware/cyclescope_system_with_bitstream.xsa | \
		rg -q '\.bit$$'
	@echo BITSTREAM_XSA_CONTENT_PASS

raw-iob-ila:
	@$(ENV) && vivado -mode batch -nolog -nojournal \
		-source scripts/build_system.tcl -tclargs --implement --raw-iob-ila \
		--adc-sample-phase $(RAW_IOB_ILA_PHASE)
	@cd "$(RAW_IOB_ILA_BUILD)/hardware" && \
		sha256sum -c "$(RAW_IOB_ILA_STEM).sha256"
	@test -s "$(RAW_IOB_ILA_BUILD)/hardware/$(RAW_IOB_ILA_STEM).manifest.json"
	@python3 -m json.tool \
		"$(RAW_IOB_ILA_BUILD)/hardware/$(RAW_IOB_ILA_STEM).manifest.json" \
		>/dev/null
	@echo RAW_IOB_ILA_ARTIFACT_PASS

raw-iob-ila-0:
	@$(MAKE) raw-iob-ila RAW_IOB_ILA_PHASE=0

raw-iob-ila-30:
	@$(MAKE) raw-iob-ila RAW_IOB_ILA_PHASE=30

raw-iob-ila-60:
	@$(MAKE) raw-iob-ila RAW_IOB_ILA_PHASE=60

raw-iob-ila-90:
	@$(MAKE) raw-iob-ila RAW_IOB_ILA_PHASE=90

raw-iob-ila-120:
	@$(MAKE) raw-iob-ila RAW_IOB_ILA_PHASE=120

raw-iob-ila-150:
	@$(MAKE) raw-iob-ila RAW_IOB_ILA_PHASE=150

raw-iob-ila-180:
	@$(MAKE) raw-iob-ila RAW_IOB_ILA_PHASE=180

raw-iob-ila-210:
	@$(MAKE) raw-iob-ila RAW_IOB_ILA_PHASE=210

raw-iob-ila-240:
	@$(MAKE) raw-iob-ila RAW_IOB_ILA_PHASE=240

raw-iob-ila-270:
	@$(MAKE) raw-iob-ila RAW_IOB_ILA_PHASE=270

raw-iob-ila-300:
	@$(MAKE) raw-iob-ila RAW_IOB_ILA_PHASE=300

raw-iob-ila-330:
	@$(MAKE) raw-iob-ila RAW_IOB_ILA_PHASE=330

raw-iob-ila-345:
	@$(MAKE) raw-iob-ila RAW_IOB_ILA_PHASE=345

raw-iob-ila-348:
	@$(MAKE) raw-iob-ila RAW_IOB_ILA_PHASE=348

raw-iob-ila-351:
	@$(MAKE) raw-iob-ila RAW_IOB_ILA_PHASE=351

raw-iob-ila-354:
	@$(MAKE) raw-iob-ila RAW_IOB_ILA_PHASE=354

raw-iob-analysis-test:
	@test -x "$(WAVEBENCH_PYTHON)"
	@"$(WAVEBENCH_PYTHON)" -m unittest discover -s tests \
		-p 'test_analyze_adc_raw.py' -v

clean:
	@$(ENV) && rm -rf "$(CURDIR)/build"
