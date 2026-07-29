create_clock -name adc_sample_clk -period 15.384 [get_ports adc_clk]
create_clock -name spi_sclk_ext  -period 100.000 [get_ports spi_sclk]
set_property HD.CLK_SRC BUFGCTRL_X0Y0 [get_ports adc_clk]
set_property HD.CLK_SRC BUFGCTRL_X0Y1 [get_ports spi_sclk]

# OOC budgets; the system project applies the board-specific source-synchronous
# ADC input timing and replaces these generic interface budgets.
set_input_delay  2.000 -clock adc_sample_clk [get_ports {adc_data_a[*] adc_otr_a m_axis_tready}]
set_output_delay 2.000 -clock adc_sample_clk [get_ports {m_axis_tdata[*] m_axis_tkeep[*] m_axis_tvalid m_axis_tlast frame_id[*] status_word[*]}]
set_input_delay  5.000 -clock spi_sclk_ext [get_ports spi_mosi]
set_output_delay 5.000 -clock spi_sclk_ext [get_ports spi_miso]

set_clock_groups -asynchronous -group [get_clocks adc_sample_clk] -group [get_clocks spi_sclk_ext]
set_false_path -from [get_ports {capture_enable clear_stats test_pattern}] \
    -to [get_cells -hier -filter {ASYNC_REG == TRUE}]
set_false_path -from [get_ports {adc_rst_n spi_cs_n}]
