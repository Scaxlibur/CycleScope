# CycleScope Z7-Nano + AD9226 channel A board constraints.

set_property PACKAGE_PIN N18 [get_ports sys_clk_50m]
set_property IOSTANDARD LVCMOS33 [get_ports sys_clk_50m]
# The typed block-design clock port creates sys_clk_50m at 50 MHz.
set_input_jitter [get_clocks sys_clk_50m] 0.200

set_property PACKAGE_PIN P14 [get_ports ext_reset_n]
set_property IOSTANDARD LVCMOS33 [get_ports ext_reset_n]
set_property PULLUP true [get_ports ext_reset_n]

set_property PACKAGE_PIN P18 [get_ports Adc_Clk_A]
set_property IOSTANDARD LVCMOS33 [get_ports Adc_Clk_A]
set_property SLEW FAST [get_ports Adc_Clk_A]

set_property PACKAGE_PIN N17 [get_ports {Adc_In_A[0]}]
set_property PACKAGE_PIN R17 [get_ports {Adc_In_A[1]}]
set_property PACKAGE_PIN R16 [get_ports {Adc_In_A[2]}]
set_property PACKAGE_PIN R18 [get_ports {Adc_In_A[3]}]
set_property PACKAGE_PIN T17 [get_ports {Adc_In_A[4]}]
set_property PACKAGE_PIN U17 [get_ports {Adc_In_A[5]}]
set_property PACKAGE_PIN T16 [get_ports {Adc_In_A[6]}]
set_property PACKAGE_PIN W19 [get_ports {Adc_In_A[7]}]
set_property PACKAGE_PIN W18 [get_ports {Adc_In_A[8]}]
set_property PACKAGE_PIN Y19 [get_ports {Adc_In_A[9]}]
set_property PACKAGE_PIN Y18 [get_ports {Adc_In_A[10]}]
set_property PACKAGE_PIN Y17 [get_ports {Adc_In_A[11]}]
set_property IOSTANDARD LVCMOS33 [get_ports {Adc_In_A[*]}]

set_property PACKAGE_PIN Y16 [get_ports Otr_A]
set_property IOSTANDARD LVCMOS33 [get_ports Otr_A]

set_property PACKAGE_PIN R14 [get_ports spi_cs_n]
set_property PACKAGE_PIN T10 [get_ports spi_sclk]
set_property PACKAGE_PIN W13 [get_ports spi_mosi]
set_property PACKAGE_PIN T15 [get_ports spi_miso]
set_property IOSTANDARD LVCMOS33 [get_ports {spi_cs_n spi_sclk spi_mosi spi_miso}]
set_property PULLUP true [get_ports spi_cs_n]
create_clock -name spi_sclk_ext -period 100.000 [get_ports spi_sclk]
set_input_delay  -clock spi_sclk_ext -min 2.000 [get_ports spi_mosi]
set_input_delay  -clock spi_sclk_ext -max 8.000 [get_ports spi_mosi]
set_output_delay -clock spi_sclk_ext -min 2.000 [get_ports spi_miso]
set_output_delay -clock spi_sclk_ext -max 8.000 [get_ports spi_miso]
set_output_delay -clock spi_sclk_ext -clock_fall -add_delay -min 2.000 \
    [get_ports spi_miso]
set_output_delay -clock spi_sclk_ext -clock_fall -add_delay -max 8.000 \
    [get_ports spi_miso]

set_false_path -from [get_ports {ext_reset_n spi_cs_n}]
