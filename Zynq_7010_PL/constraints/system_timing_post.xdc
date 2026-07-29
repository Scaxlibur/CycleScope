# These constraints reference PS7 and Clock Wizard clocks that only exist after
# Vivado merges the out-of-context IP checkpoints. build_system.tcl therefore
# marks this file implementation-only; open_run synth_1 still applies it before
# producing the integrated timing and CDC reports.

# AD9226 tOD is 3.5--7 ns after the conversion-clock rising edge. The 300-degree
# sample phase is near the center of the valid window at 65 MHz.
set_input_delay \
    -clock [get_clocks -of_objects \
        [get_pins cyclescope_system_i/cyclescope_clocking_0/inst/mmcm_adv_inst/CLKOUT0]] \
    -min 3.500 [get_ports {Adc_In_A[*] Otr_A}]
set_input_delay \
    -clock [get_clocks -of_objects \
        [get_pins cyclescope_system_i/cyclescope_clocking_0/inst/mmcm_adv_inst/CLKOUT0]] \
    -max 7.000 [get_ports {Adc_In_A[*] Otr_A}]

# Point-to-point exceptions cover only the first stage of our explicit
# synchronizers. Do not use global asynchronous clock groups here: the AXIS
# Clock Converter's XPM FIFO supplies tighter max-delay and bus-skew checks.
set_false_path -to [get_pins -hier -regexp \
    {.*(capture_enable_meta_reg|clear_stats_meta_reg|test_pattern_meta_reg|request_meta_reg|acknowledge_meta_reg|data_meta_reg|spi_bank_meta_reg|spi_frame_gray_meta_reg|spi_status_meta_reg).*\/D}]
