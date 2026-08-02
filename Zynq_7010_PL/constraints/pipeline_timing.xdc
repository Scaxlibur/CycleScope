create_clock -name adc_sample_clk -period 15.384 [get_ports adc_clk]
set_property HD.CLK_SRC BUFGCTRL_X0Y0 [get_ports adc_clk]

# OOC budgets; the system project applies the board-specific source-synchronous
# ADC input timing and replaces these generic interface budgets.
set_input_delay  2.000 -clock adc_sample_clk [get_ports {adc_data_a[*] adc_otr_a m_axis_tready}]
set_output_delay 2.000 -clock adc_sample_clk [get_ports {m_axis_tdata[*] m_axis_tkeep[*] m_axis_tvalid m_axis_tlast frame_id[*] status_word[*] frame_timestamp_tick[*] adc_tick[*]}]

set_false_path -from [get_ports {capture_enable clear_stats test_pattern test_mode[*] test_amplitude[*] test_phase_increment[*] inject_otr_toggle inject_overflow_toggle inject_frame_drop_toggle}] \
    -to [get_cells -hier -filter {ASYNC_REG == TRUE}]
set_false_path -from [get_ports adc_rst_n]
set_false_path -from [get_ports spi_cs_n] \
    -to [get_pins -hier -regexp {.*spi_cs_n_meta_reg.*\/D}]
set_false_path -from [get_ports spi_sclk] \
    -to [get_pins -hier -regexp {.*spi_sclk_meta_reg.*\/D}]
set_false_path -from [get_ports spi_mosi] \
    -to [get_pins -hier -regexp {.*spi_mosi_meta_reg.*\/D}]
set_false_path -to [get_ports spi_miso]
