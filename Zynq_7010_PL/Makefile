SHELL := /bin/bash
ROOT := $(abspath ..)
ENV := source "$(CURDIR)/scripts/xilinx_env.sh"

.PHONY: version sim synth system clean

version:
	@$(ENV) && vivado -version | sed -n '1,5p' && v++ --version 2>&1 | sed -n '1,7p'

sim:
	@$(ENV) && vivado -mode batch -nolog -nojournal -source scripts/run_sim.tcl

synth:
	@$(ENV) && vivado -mode batch -nolog -nojournal -source scripts/run_synth.tcl

system:
	@$(ENV) && vivado -mode batch -nolog -nojournal -source scripts/build_system.tcl

clean:
	@$(ENV) && rm -rf "$(CURDIR)/build"
