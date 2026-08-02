# CycleScope Z7-Nano + AD9226 channel A board constraints.

set_property PACKAGE_PIN N18 [get_ports sys_clk_50m]
set_property IOSTANDARD LVCMOS33 [get_ports sys_clk_50m]
# The typed block-design clock port creates sys_clk_50m at 50 MHz.
set_input_jitter [get_clocks sys_clk_50m] 0.200

set_property PACKAGE_PIN P14 [get_ports ext_reset_n]
set_property IOSTANDARD LVCMOS33 [get_ports ext_reset_n]
set_property PULLTYPE PULLUP [get_ports ext_reset_n]

set_property PACKAGE_PIN H16 [get_ports Adc_Clk_A]
set_property IOSTANDARD LVCMOS33 [get_ports Adc_Clk_A]
set_property SLEW FAST [get_ports Adc_Clk_A]

set_property PACKAGE_PIN H17 [get_ports {Adc_In_A[0]}]
set_property PACKAGE_PIN B19 [get_ports {Adc_In_A[1]}]
set_property PACKAGE_PIN A20 [get_ports {Adc_In_A[2]}]
set_property PACKAGE_PIN C20 [get_ports {Adc_In_A[3]}]
set_property PACKAGE_PIN B20 [get_ports {Adc_In_A[4]}]
set_property PACKAGE_PIN D19 [get_ports {Adc_In_A[5]}]
set_property PACKAGE_PIN D20 [get_ports {Adc_In_A[6]}]
set_property PACKAGE_PIN J18 [get_ports {Adc_In_A[7]}]
set_property PACKAGE_PIN H18 [get_ports {Adc_In_A[8]}]
set_property PACKAGE_PIN F19 [get_ports {Adc_In_A[9]}]
set_property PACKAGE_PIN F20 [get_ports {Adc_In_A[10]}]
set_property PACKAGE_PIN G19 [get_ports {Adc_In_A[11]}]
set_property IOSTANDARD LVCMOS33 [get_ports {Adc_In_A[*]}]

set_property PACKAGE_PIN G20 [get_ports Otr_A]
set_property IOSTANDARD LVCMOS33 [get_ports Otr_A]

set_property PACKAGE_PIN R14 [get_ports spi_cs_n]
set_property PACKAGE_PIN T10 [get_ports spi_sclk]
set_property PACKAGE_PIN W13 [get_ports spi_mosi]
set_property PACKAGE_PIN T15 [get_ports spi_miso]
set_property IOSTANDARD LVCMOS33 [get_ports spi_cs_n]
set_property IOSTANDARD LVCMOS33 [get_ports spi_sclk]
set_property IOSTANDARD LVCMOS33 [get_ports spi_mosi]
set_property IOSTANDARD LVCMOS33 [get_ports spi_miso]
set_property PULLTYPE PULLUP [get_ports spi_cs_n]

set_false_path -from [get_ports ext_reset_n]
set_false_path -to [get_ports spi_miso]
