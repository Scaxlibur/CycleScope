# These constraints reference PS7 and Clock Wizard clocks that only exist after
# Vivado merges the out-of-context IP checkpoints. build_system.tcl therefore
# marks this file implementation-only; open_run synth_1 still applies it before
# producing the integrated timing and CDC reports.

# AD9226 tOD is 3.5--7 ns after the conversion-clock rising edge. The 210-degree
# production phase samples about 8.97 ns after that edge at 65 MHz. Current-board
# raw-IOB captures found this phase stable; 300 degrees sampled single-cycle
# ORA/data transition glitches despite meeting the simplified external model.
set_input_delay -clock [get_clocks -of_objects [get_pins cyclescope_system_i/cyclescope_clocking_0/inst/mmcm_adv_inst/CLKOUT0]] -min 3.500 [get_ports {{Adc_In_A[*]} Otr_A}]
set_input_delay -clock [get_clocks -of_objects [get_pins cyclescope_system_i/cyclescope_clocking_0/inst/mmcm_adv_inst/CLKOUT0]] -max 7.000 [get_ports {{Adc_In_A[*]} Otr_A}]

# Point-to-point exceptions cover only the first stage of our explicit
# synchronizers. Do not use global asynchronous clock groups here: the AXIS
# Clock Converter's XPM FIFO supplies tighter max-delay and bus-skew checks.
set_false_path -to [get_pins -hier -regexp {.*(capture_enable_meta_reg|clear_stats_meta_reg|test_pattern_meta_reg|test_mode_meta_reg|test_amplitude_meta_reg|test_phase_increment_meta_reg|inject_otr_meta_reg|inject_overflow_meta_reg|inject_frame_drop_meta_reg|request_meta_reg|acknowledge_meta_reg|data_meta_reg).*\/D}]
set_false_path -from [get_ports spi_cs_n] -to [get_pins -hier -regexp {.*spi_cs_n_meta_reg.*\/D}]
set_false_path -from [get_ports spi_sclk] -to [get_pins -hier -regexp {.*spi_sclk_meta_reg.*\/D}]
set_false_path -from [get_ports spi_mosi] -to [get_pins -hier -regexp {.*spi_mosi_meta_reg.*\/D}]
