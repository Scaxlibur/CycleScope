create_clock -name adc_sample_clk -period 15.384 [get_ports clk]
set_property HD.CLK_SRC BUFGCTRL_X0Y0 [get_ports clk]

# Out-of-context interface budget. The integrated design replaces these values
# with the real upstream/downstream timing and board-level ADC constraints.
set_input_delay  2.000 -clock adc_sample_clk [get_ports {s_valid s_sample[*] s_otr}]
set_output_delay 2.000 -clock adc_sample_clk [get_ports {m_valid m_sample[*] m_otr schedule_error}]

# Reset is asynchronous assertion with synchronous release inside the design.
set_false_path -from [get_ports rst_n]
