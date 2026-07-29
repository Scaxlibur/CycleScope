set script_dir [file dirname [file normalize [info script]]]
set pl_root [file normalize [file join $script_dir ..]]
set build_root [file join $pl_root build system]
set project_root [file join $build_root project]
set report_root [file join $build_root reports]
set hardware_root [file join $build_root hardware]

file delete -force $build_root
file mkdir $project_root
file mkdir $report_root
file mkdir $hardware_root

create_project -force cyclescope_system $project_root -part xc7z010clg400-1
set_property target_language Verilog [current_project]
set_property simulator_language Mixed [current_project]

set rtl_sources [list \
    [file join $pl_root rtl fir_coeffs_pkg.sv] \
    [file join $pl_root rtl ad9226_frontend.sv] \
    [file join $pl_root rtl fir_mac_decimator.sv] \
    [file join $pl_root rtl fir_decimator_16.sv] \
    [file join $pl_root rtl frame_ram.sv] \
    [file join $pl_root rtl frame_store_axis_spi.sv] \
    [file join $pl_root rtl status_snapshot_cdc.sv] \
    [file join $pl_root rtl cyclescope_pipeline.sv] \
    [file join $pl_root rtl cyclescope_pipeline_bd.v] \
    [file join $pl_root rtl status_snapshot_cdc_bd.v]]
add_files -norecurse $rtl_sources
add_files -fileset constrs_1 -norecurse [file join $pl_root constraints system.xdc]
set post_timing_xdc [file join $pl_root constraints system_timing_post.xdc]
add_files -fileset constrs_1 -norecurse $post_timing_xdc
set_property USED_IN_SYNTHESIS false [get_files $post_timing_xdc]
set_property file_type SystemVerilog [get_files -of_objects [get_filesets sources_1] *.sv]
update_compile_order -fileset sources_1

create_bd_design cyclescope_system

# Z7-Nano PS: one x16 MT41K256M16 DDR3, 33.333 MHz PS clock, RGMII
# Ethernet, QSPI boot flash, UART0 on MIO14/15, GP0 control and HP0 DMA.
set ps7 [create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 processing_system7_0]
set_property -dict [list \
    CONFIG.PCW_IMPORT_BOARD_PRESET {None} \
    CONFIG.PCW_PACKAGE_NAME {clg400} \
    CONFIG.PCW_PRESET_BANK0_VOLTAGE {LVCMOS 3.3V} \
    CONFIG.PCW_PRESET_BANK1_VOLTAGE {LVCMOS 1.8V} \
    CONFIG.PCW_CRYSTAL_PERIPHERAL_FREQMHZ {33.333333} \
    CONFIG.PCW_EN_DDR {1} \
    CONFIG.PCW_UIPARAM_DDR_PARTNO {MT41K256M16 RE-125} \
    CONFIG.PCW_UIPARAM_DDR_DEVICE_CAPACITY {4096 MBits} \
    CONFIG.PCW_UIPARAM_DDR_BUS_WIDTH {16 Bit} \
    CONFIG.PCW_UIPARAM_DDR_DRAM_WIDTH {16 Bits} \
    CONFIG.PCW_UIPARAM_DDR_SPEED_BIN {DDR3_1066F} \
    CONFIG.PCW_QSPI_PERIPHERAL_ENABLE {1} \
    CONFIG.PCW_QSPI_GRP_SINGLE_SS_ENABLE {1} \
    CONFIG.PCW_QSPI_GRP_SINGLE_SS_IO {MIO 1 .. 6} \
    CONFIG.PCW_UART0_PERIPHERAL_ENABLE {1} \
    CONFIG.PCW_UART0_UART0_IO {MIO 14 .. 15} \
    CONFIG.PCW_ENET0_PERIPHERAL_ENABLE {1} \
    CONFIG.PCW_ENET0_ENET0_IO {MIO 16 .. 27} \
    CONFIG.PCW_ENET0_GRP_MDIO_ENABLE {1} \
    CONFIG.PCW_ENET0_GRP_MDIO_IO {MIO 52 .. 53} \
    CONFIG.PCW_USE_M_AXI_GP0 {1} \
    CONFIG.PCW_USE_S_AXI_HP0 {1} \
    CONFIG.PCW_USE_FABRIC_INTERRUPT {1} \
    CONFIG.PCW_IRQ_F2P_INTR {1} \
    CONFIG.PCW_EN_CLK0_PORT {1} \
    CONFIG.PCW_EN_RST0_PORT {1} \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {100.000000}] $ps7

# Setting PCW_PACKAGE_NAME for clg400 re-applies the IP's generic 32-bit DDR
# pin defaults, so override the physical bus widths in a second transaction.
set_property -dict [list \
    CONFIG.PCW_DQ_WIDTH {16} \
    CONFIG.PCW_DQS_WIDTH {2} \
    CONFIG.PCW_DM_WIDTH {2}] $ps7

foreach {property expected} [list \
    CONFIG.PCW_UIPARAM_DDR_BUS_WIDTH {16 Bit} \
    CONFIG.PCW_DQ_WIDTH {16} \
    CONFIG.PCW_DQS_WIDTH {2} \
    CONFIG.PCW_DM_WIDTH {2}] {
    set actual [get_property $property $ps7]
    if {$actual ne $expected} {
        error "PS7 DDR configuration mismatch: $property=$actual, expected $expected"
    }
}

make_bd_intf_pins_external [get_bd_intf_pins $ps7/DDR]
set_property name DDR [get_bd_intf_ports DDR_0]
make_bd_intf_pins_external [get_bd_intf_pins $ps7/FIXED_IO]
set_property name FIXED_IO [get_bd_intf_ports FIXED_IO_0]

# PL clock generator: 50 MHz board oscillator -> 65 MHz conversion clock and
# 65 MHz sample clock delayed 300 degrees, centered in the AD9226 tOD window.
set clocking [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz:6.0 cyclescope_clocking_0]
set_property -dict [list \
    CONFIG.PRIM_SOURCE {Single_ended_clock_capable_pin} \
    CONFIG.PRIM_IN_FREQ {50.000} \
    CONFIG.CLKOUT1_REQUESTED_OUT_FREQ {65.000} \
    CONFIG.CLKOUT1_REQUESTED_PHASE {0.000} \
    CONFIG.CLKOUT2_USED {true} \
    CONFIG.CLKOUT2_REQUESTED_OUT_FREQ {65.000} \
    CONFIG.CLKOUT2_REQUESTED_PHASE {300.000} \
    CONFIG.NUM_OUT_CLKS {2} \
    CONFIG.USE_RESET {true} \
    CONFIG.RESET_TYPE {ACTIVE_LOW} \
    CONFIG.USE_LOCKED {true}] $clocking

create_bd_port -dir I -type clk -freq_hz 50000000 sys_clk_50m
create_bd_port -dir I -type rst ext_reset_n
set_property CONFIG.POLARITY ACTIVE_LOW [get_bd_ports ext_reset_n]
create_bd_port -dir O -type clk Adc_Clk_A
connect_bd_net [get_bd_ports sys_clk_50m] [get_bd_pins $clocking/clk_in1]
connect_bd_net [get_bd_ports ext_reset_n] [get_bd_pins $clocking/resetn]
connect_bd_net [get_bd_pins $clocking/clk_out1] [get_bd_ports Adc_Clk_A]

# Reset synchronizers for the independent PS FCLK and ADC sample domains.
set const_zero [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 const_zero]
set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {0}] $const_zero
set const_one [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 const_one]
set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {1}] $const_one

set invert_fclk_reset [create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic:2.0 invert_fclk_reset]
set_property -dict [list CONFIG.C_SIZE {1} CONFIG.C_OPERATION {not}] $invert_fclk_reset
connect_bd_net [get_bd_pins $ps7/FCLK_RESET0_N] [get_bd_pins $invert_fclk_reset/Op1]

set invert_ext_reset [create_bd_cell -type ip -vlnv xilinx.com:ip:util_vector_logic:2.0 invert_ext_reset]
set_property -dict [list CONFIG.C_SIZE {1} CONFIG.C_OPERATION {not}] $invert_ext_reset
connect_bd_net [get_bd_ports ext_reset_n] [get_bd_pins $invert_ext_reset/Op1]

set reset_fclk [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 reset_fclk]
connect_bd_net [get_bd_pins $ps7/FCLK_CLK0] [get_bd_pins $reset_fclk/slowest_sync_clk]
connect_bd_net [get_bd_pins $invert_fclk_reset/Res] [get_bd_pins $reset_fclk/ext_reset_in]
connect_bd_net [get_bd_pins $invert_ext_reset/Res] [get_bd_pins $reset_fclk/aux_reset_in]
connect_bd_net [get_bd_pins $const_zero/dout] [get_bd_pins $reset_fclk/mb_debug_sys_rst]
connect_bd_net [get_bd_pins $const_one/dout] [get_bd_pins $reset_fclk/dcm_locked]

set reset_adc [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 reset_adc]
connect_bd_net [get_bd_pins $clocking/clk_out2] [get_bd_pins $reset_adc/slowest_sync_clk]
connect_bd_net [get_bd_pins $invert_fclk_reset/Res] [get_bd_pins $reset_adc/ext_reset_in]
connect_bd_net [get_bd_pins $invert_ext_reset/Res] [get_bd_pins $reset_adc/aux_reset_in]
connect_bd_net [get_bd_pins $const_zero/dout] [get_bd_pins $reset_adc/mb_debug_sys_rst]
connect_bd_net [get_bd_pins $clocking/locked] [get_bd_pins $reset_adc/dcm_locked]

# Simple S2MM DMA. AXI DMA 7.1 clocks S_AXIS_S2MM and M_AXI_S2MM from the
# same m_axi_s2mm_aclk, so a dedicated AXIS Clock Converter owns the
# 65-to-100 MHz crossing before the DMA.
set dma [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 axi_dma_0]
set_property -dict [list \
    CONFIG.c_include_sg {0} \
    CONFIG.c_include_mm2s {0} \
    CONFIG.c_include_s2mm {1} \
    CONFIG.c_addr_width {32} \
    CONFIG.c_sg_length_width {23} \
    CONFIG.c_m_axi_s2mm_data_width {64} \
    CONFIG.c_s_axis_s2mm_tdata_width {16} \
    CONFIG.c_s2mm_burst_size {16} \
    CONFIG.c_include_s2mm_dre {0}] $dma

connect_bd_net [get_bd_pins $ps7/FCLK_CLK0] \
    [get_bd_pins $ps7/M_AXI_GP0_ACLK] \
    [get_bd_pins $ps7/S_AXI_HP0_ACLK] \
    [get_bd_pins $dma/s_axi_lite_aclk] \
    [get_bd_pins $dma/m_axi_s2mm_aclk]
connect_bd_net [get_bd_pins $reset_fclk/peripheral_aresetn] [get_bd_pins $dma/axi_resetn]

# PS7 HP ports are AXI3 while AXI DMA emits AXI4; SmartConnect performs the
# protocol conversion and keeps the DDR path at 64 bits.
set hp0 [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 hp0_smartconnect]
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {1}] $hp0
connect_bd_net [get_bd_pins $ps7/FCLK_CLK0] [get_bd_pins $hp0/aclk]
connect_bd_net [get_bd_pins $reset_fclk/interconnect_aresetn] [get_bd_pins $hp0/aresetn]
connect_bd_intf_net [get_bd_intf_pins $dma/M_AXI_S2MM] [get_bd_intf_pins $hp0/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins $hp0/M00_AXI] [get_bd_intf_pins $ps7/S_AXI_HP0]
connect_bd_net [get_bd_pins $dma/s2mm_introut] [get_bd_pins $ps7/IRQ_F2P]

# One GP0 fanout serves DMA registers, control GPIO, and coherent status GPIO.
set gp0 [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 gp0_smartconnect]
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {3}] $gp0
connect_bd_net [get_bd_pins $ps7/FCLK_CLK0] [get_bd_pins $gp0/aclk]
connect_bd_net [get_bd_pins $reset_fclk/interconnect_aresetn] [get_bd_pins $gp0/aresetn]
connect_bd_intf_net [get_bd_intf_pins $ps7/M_AXI_GP0] [get_bd_intf_pins $gp0/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins $gp0/M00_AXI] [get_bd_intf_pins $dma/S_AXI_LITE]

set gpio_control [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 gpio_control]
set_property -dict [list \
    CONFIG.C_GPIO_WIDTH {32} \
    CONFIG.C_ALL_OUTPUTS {1} \
    CONFIG.C_DOUT_DEFAULT {0x00000000}] $gpio_control
set gpio_status [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 gpio_status]
set_property -dict [list \
    CONFIG.C_IS_DUAL {1} \
    CONFIG.C_GPIO_WIDTH {32} \
    CONFIG.C_GPIO2_WIDTH {32} \
    CONFIG.C_ALL_INPUTS {1} \
    CONFIG.C_ALL_INPUTS_2 {1}] $gpio_status

foreach gpio [list $gpio_control $gpio_status] {
    connect_bd_net [get_bd_pins $ps7/FCLK_CLK0] [get_bd_pins $gpio/s_axi_aclk]
    connect_bd_net [get_bd_pins $reset_fclk/peripheral_aresetn] [get_bd_pins $gpio/s_axi_aresetn]
}
connect_bd_intf_net [get_bd_intf_pins $gp0/M01_AXI] [get_bd_intf_pins $gpio_control/S_AXI]
connect_bd_intf_net [get_bd_intf_pins $gp0/M02_AXI] [get_bd_intf_pins $gpio_status/S_AXI]

# Source RTL enters the block design as module references.
set pipeline [create_bd_cell -type module -reference cyclescope_pipeline_bd cyclescope_pipeline_0]
set status_cdc [create_bd_cell -type module -reference status_snapshot_cdc_bd status_snapshot_cdc_0]

connect_bd_net [get_bd_pins $clocking/clk_out2] \
    [get_bd_pins $pipeline/adc_clk] \
    [get_bd_pins $status_cdc/src_clk]
connect_bd_net [get_bd_pins $reset_adc/peripheral_aresetn] \
    [get_bd_pins $pipeline/adc_rst_n] \
    [get_bd_pins $status_cdc/src_rst_n]
connect_bd_net [get_bd_pins $ps7/FCLK_CLK0] [get_bd_pins $status_cdc/dst_clk]
connect_bd_net [get_bd_pins $reset_fclk/peripheral_aresetn] [get_bd_pins $status_cdc/dst_rst_n]

create_bd_port -dir I -from 11 -to 0 Adc_In_A
create_bd_port -dir I Otr_A
create_bd_port -dir I spi_cs_n
create_bd_port -dir I -type clk -freq_hz 10000000 spi_sclk
create_bd_port -dir I spi_mosi
create_bd_port -dir O spi_miso
connect_bd_net [get_bd_ports Adc_In_A] [get_bd_pins $pipeline/adc_data_a]
connect_bd_net [get_bd_ports Otr_A] [get_bd_pins $pipeline/adc_otr_a]
connect_bd_net [get_bd_ports spi_cs_n] [get_bd_pins $pipeline/spi_cs_n]
connect_bd_net [get_bd_ports spi_sclk] [get_bd_pins $pipeline/spi_sclk]
connect_bd_net [get_bd_ports spi_mosi] [get_bd_pins $pipeline/spi_mosi]
connect_bd_net [get_bd_ports spi_miso] [get_bd_pins $pipeline/spi_miso]

set axis_cdc [create_bd_cell -type ip -vlnv xilinx.com:ip:axis_clock_converter:1.1 axis_clock_converter_0]
set_property -dict [list \
    CONFIG.TDATA_NUM_BYTES {2} \
    CONFIG.HAS_TKEEP {1} \
    CONFIG.HAS_TLAST {1}] $axis_cdc
connect_bd_net [get_bd_pins $clocking/clk_out2] [get_bd_pins $axis_cdc/s_axis_aclk]
connect_bd_net [get_bd_pins $reset_adc/peripheral_aresetn] [get_bd_pins $axis_cdc/s_axis_aresetn]
connect_bd_net [get_bd_pins $ps7/FCLK_CLK0] [get_bd_pins $axis_cdc/m_axis_aclk]
connect_bd_net [get_bd_pins $reset_fclk/peripheral_aresetn] [get_bd_pins $axis_cdc/m_axis_aresetn]
connect_bd_intf_net [get_bd_intf_pins $pipeline/m_axis] [get_bd_intf_pins $axis_cdc/S_AXIS]
connect_bd_intf_net [get_bd_intf_pins $axis_cdc/M_AXIS] [get_bd_intf_pins $dma/S_AXIS_S2MM]

# GPIO control bits 0..2 map to capture_enable, clear_stats and test_pattern.
for {set bit_index 0} {$bit_index < 3} {incr bit_index} {
    set control_slice($bit_index) [create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 control_slice_$bit_index]
    set_property -dict [list \
        CONFIG.DIN_WIDTH {32} \
        CONFIG.DIN_FROM $bit_index \
        CONFIG.DIN_TO $bit_index \
        CONFIG.DOUT_WIDTH {1}] $control_slice($bit_index)
    connect_bd_net [get_bd_pins $gpio_control/gpio_io_o] [get_bd_pins $control_slice($bit_index)/Din]
}
connect_bd_net [get_bd_pins $control_slice(0)/Dout] [get_bd_pins $pipeline/capture_enable]
connect_bd_net [get_bd_pins $control_slice(1)/Dout] [get_bd_pins $pipeline/clear_stats]
connect_bd_net [get_bd_pins $control_slice(2)/Dout] [get_bd_pins $pipeline/test_pattern]

# Snapshot {frame_id,status_word} coherently into FCLK0 before exposing it.
set status_concat [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat:2.1 status_concat]
set_property -dict [list \
    CONFIG.NUM_PORTS {2} \
    CONFIG.IN0_WIDTH {32} \
    CONFIG.IN1_WIDTH {32}] $status_concat
connect_bd_net [get_bd_pins $pipeline/status_word] [get_bd_pins $status_concat/In0]
connect_bd_net [get_bd_pins $pipeline/frame_id] [get_bd_pins $status_concat/In1]
connect_bd_net [get_bd_pins $status_concat/dout] [get_bd_pins $status_cdc/src_data]

set status_slice [create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 status_slice]
set_property -dict [list CONFIG.DIN_WIDTH {64} CONFIG.DIN_FROM {31} CONFIG.DIN_TO {0} CONFIG.DOUT_WIDTH {32}] $status_slice
set frame_id_slice [create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 frame_id_slice]
set_property -dict [list CONFIG.DIN_WIDTH {64} CONFIG.DIN_FROM {63} CONFIG.DIN_TO {32} CONFIG.DOUT_WIDTH {32}] $frame_id_slice
connect_bd_net [get_bd_pins $status_cdc/dst_data] \
    [get_bd_pins $status_slice/Din] \
    [get_bd_pins $frame_id_slice/Din]
connect_bd_net [get_bd_pins $status_slice/Dout] [get_bd_pins $gpio_status/gpio_io_i]
connect_bd_net [get_bd_pins $frame_id_slice/Dout] [get_bd_pins $gpio_status/gpio2_io_i]

assign_bd_address
validate_bd_design

set ddr_high [get_property CONFIG.PCW_DDR_RAM_HIGHADDR $ps7]
if {$ddr_high ne "0x1FFFFFFF"} {
    error "PS7 DDR address range mismatch: high=$ddr_high, expected 0x1FFFFFFF for 512 MiB"
}
puts "PS7_DDR_DQ=16 DQS=2 DM=2 HIGHADDR=$ddr_high"
save_bd_design

set bd_file [get_files [file join $project_root cyclescope_system.srcs sources_1 bd cyclescope_system cyclescope_system.bd]]
generate_target all $bd_file
set wrapper_file [make_wrapper -files $bd_file -top]
add_files -norecurse $wrapper_file
set_property top cyclescope_system_wrapper [get_filesets sources_1]
update_compile_order -fileset sources_1

launch_runs synth_1 -jobs 8
wait_on_run synth_1
if {[get_property STATUS [get_runs synth_1]] ne "synth_design Complete!"} {
    error "system synthesis failed: [get_property STATUS [get_runs synth_1]]"
}
open_run synth_1

report_utilization -file [file join $report_root utilization.rpt]
report_timing_summary -delay_type min_max -report_unconstrained -check_timing_verbose \
    -file [file join $report_root timing_summary.rpt]
set cdc_clocks [get_clocks [list \
    clk_fpga_0 \
    clk_out2_cyclescope_system_cyclescope_clocking_0_0 \
    spi_sclk_ext]]
set cdc_text [report_cdc -details -from $cdc_clocks -to $cdc_clocks -return_string]
set cdc_file [open [file join $report_root cdc.rpt] w]
puts $cdc_file $cdc_text
close $cdc_file
if {[regexp {CDC-[0-9]+[[:space:]]+Critical} $cdc_text]} {
    error "critical CDC finding detected; inspect reports/cdc.rpt"
}
report_bus_skew -file [file join $report_root bus_skew.rpt]
report_methodology -file [file join $report_root methodology.rpt]
write_checkpoint -force [file join $build_root cyclescope_system_synth.dcp]

# M4 exports the hardware handoff without a bitstream because this development
# pass is deliberately limited to simulation, synthesis and software compile.
write_hw_platform -fixed -force -file [file join $hardware_root cyclescope_system.xsa]

set worst_path [get_timing_paths -delay_type max -max_paths 1]
if {[llength $worst_path] == 0} {
    error "no timing path found after system synthesis"
}
set worst_slack [get_property SLACK [lindex $worst_path 0]]
puts "SYSTEM_SYNTH_WORST_SLACK_NS=$worst_slack"
puts "SYSTEM_XSA=[file join $hardware_root cyclescope_system.xsa]"
if {$worst_slack < 0.0} {
    error "system synthesis timing failed: slack=$worst_slack ns"
}

puts "SYSTEM_SYNTH_PASS"
exit
