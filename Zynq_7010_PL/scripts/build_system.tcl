proc require_one {label objects} {
    if {[llength $objects] != 1} {
        error "$label expected exactly one object, got [llength $objects]: $objects"
    }
    return [lindex $objects 0]
}

proc parse_build_options {arguments} {
    set options [dict create \
        implement 0 \
        raw_iob_ila 0 \
        adc_sample_phase 210]
    set phase_was_set 0

    for {set index 0} {$index < [llength $arguments]} {incr index} {
        set argument [lindex $arguments $index]
        switch -- $argument {
            --implement {
                dict set options implement 1
            }
            --raw-iob-ila {
                dict set options raw_iob_ila 1
            }
            --adc-sample-phase {
                incr index
                if {$index >= [llength $arguments]} {
                    error "--adc-sample-phase requires a supported full-cycle diagnostic phase"
                }
                set phase [lindex $arguments $index]
                set supported_phases {0 30 60 90 120 150 180 210 240 270 300 330 345 348 351 354}
                if {[lsearch -exact $supported_phases $phase] < 0} {
                    error "unsupported ADC sample phase $phase; expected one of $supported_phases"
                }
                dict set options adc_sample_phase $phase
                set phase_was_set 1
            }
            default {
                error "unknown build argument: $argument"
            }
        }
    }

    if {[dict get $options raw_iob_ila] && ![dict get $options implement]} {
        error "--raw-iob-ila requires --implement"
    }
    if {$phase_was_set && ![dict get $options raw_iob_ila]} {
        error "--adc-sample-phase is diagnostic-only and requires --raw-iob-ila"
    }
    return $options
}

proc adc_data_iob_cell {bit} {
    set pattern [format \
        {^.*/cyclescope_pipeline_0/inst/pipeline_i/adc_data_a_iob_reg\[%d\]$} \
        $bit]
    set cell [require_one "ADC data IOB cell $bit" \
        [get_cells -quiet -hierarchical -regexp $pattern]]
    if {[get_property REF_NAME $cell] ne "FDRE"} {
        error "ADC data IOB cell $bit is not FDRE: $cell"
    }
    return $cell
}

proc adc_otr_iob_cell {} {
    set cell [require_one "ADC OTR IOB cell" [get_cells -quiet -hierarchical \
        -regexp {^.*/cyclescope_pipeline_0/inst/pipeline_i/adc_otr_a_iob_reg$}]]
    if {[get_property REF_NAME $cell] ne "FDRE"} {
        error "ADC OTR IOB cell is not FDRE: $cell"
    }
    return $cell
}

proc cell_pin {label cell ref_pin_name} {
    return [require_one "$label $ref_pin_name pin" [get_pins -quiet \
        -of_objects $cell -filter "REF_PIN_NAME == $ref_pin_name"]]
}

proc pin_net {label pin} {
    return [require_one "$label net" [get_nets -quiet -of_objects $pin]]
}

set script_dir [file dirname [file normalize [info script]]]
set pl_root [file normalize [file join $script_dir ..]]
set build_options [parse_build_options $argv]
set do_implementation [dict get $build_options implement]
set do_raw_iob_ila [dict get $build_options raw_iob_ila]
set adc_sample_phase [dict get $build_options adc_sample_phase]
if {$do_raw_iob_ila} {
    set build_root [file join $pl_root build diagnostic \
        raw-iob-ila-p${adc_sample_phase}]
} else {
    set build_root [file join $pl_root build system]
}
set project_root [file join $build_root project]
set report_root [file join $build_root reports]
set hardware_root [file join $build_root hardware]

puts "SYSTEM_BUILD_MODE=[expr {$do_implementation ? "implementation" : "synthesis"}]"
puts "RAW_IOB_ILA_MODE=[expr {$do_raw_iob_ila ? "enabled" : "disabled"}]"
puts "ADC_SAMPLE_PHASE_DEG=$adc_sample_phase"

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
    [file join $pl_root rtl test_pattern_generator.sv] \
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
set raw_ila_xdc ""
if {$do_raw_iob_ila} {
    set raw_ila_xdc [file join $build_root raw_iob_ila_debug.xdc]
    set raw_ila_xdc_channel [open $raw_ila_xdc w]
    close $raw_ila_xdc_channel
    add_files -fileset constrs_1 -norecurse $raw_ila_xdc
    set raw_ila_xdc_file [get_files $raw_ila_xdc]
    set_property USED_IN_SYNTHESIS false $raw_ila_xdc_file
    set_property USED_IN_IMPLEMENTATION true $raw_ila_xdc_file
    set_property PROCESSING_ORDER LATE $raw_ila_xdc_file
    set_property target_constrs_file $raw_ila_xdc_file [get_filesets constrs_1]
}
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
    CONFIG.PCW_ENET0_PERIPHERAL_CLKSRC {IO PLL} \
    CONFIG.PCW_ENET0_PERIPHERAL_FREQMHZ {100 Mbps} \
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
    CONFIG.PCW_DM_WIDTH {2} \
    CONFIG.PCW_ENET0_PERIPHERAL_CLKSRC {IO PLL} \
    CONFIG.PCW_ENET0_PERIPHERAL_FREQMHZ {100 Mbps}] {
    set actual [get_property $property $ps7]
    if {$actual ne $expected} {
        error "PS7 configuration mismatch: $property=$actual, expected $expected"
    }
}

make_bd_intf_pins_external [get_bd_intf_pins $ps7/DDR]
set_property name DDR [get_bd_intf_ports DDR_0]
make_bd_intf_pins_external [get_bd_intf_pins $ps7/FIXED_IO]
set_property name FIXED_IO [get_bd_intf_ports FIXED_IO_0]

# PL clock generator: 50 MHz board oscillator -> 65 MHz conversion clock and
# a phase-shifted 65 MHz sample clock. Production uses 210 degrees after the
# current board-level raw-IOB capture showed that 300 degrees can sample
# single-cycle ORA/data transition glitches. Raw-IOB diagnostics may select
# coarse full-cycle phases plus the existing
# 345/348/351/354-degree transition-edge probes. Every diagnostic phase must
# independently pass the unchanged 1 ns modeled setup/hold gate; merely
# appearing in the allow-list never authorizes a download. The existing model
# excludes 357 degrees.
set clocking [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz:6.0 cyclescope_clocking_0]
set_property -dict [list \
    CONFIG.PRIM_SOURCE {Single_ended_clock_capable_pin} \
    CONFIG.PRIM_IN_FREQ {50.000} \
    CONFIG.CLKOUT1_REQUESTED_OUT_FREQ {65.000} \
    CONFIG.CLKOUT1_REQUESTED_PHASE {0.000} \
    CONFIG.CLKOUT2_USED {true} \
    CONFIG.CLKOUT2_REQUESTED_OUT_FREQ {65.000} \
    CONFIG.CLKOUT2_REQUESTED_PHASE $adc_sample_phase \
    CONFIG.NUM_OUT_CLKS {2} \
    CONFIG.USE_RESET {true} \
    CONFIG.RESET_TYPE {ACTIVE_LOW} \
    CONFIG.USE_LOCKED {true}] $clocking
set configured_adc_phase [get_property CONFIG.CLKOUT2_REQUESTED_PHASE $clocking]
if {[expr {abs(double($configured_adc_phase) - double($adc_sample_phase))}] > 0.001} {
    error "Clock Wizard ADC sample phase mismatch: $configured_adc_phase"
}

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

# One GP0 fanout serves DMA registers, control GPIO, and the coherent frame/
# timestamp/ADC-clock snapshot exposed to the LAN firmware.
set gp0 [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 gp0_smartconnect]
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {5}] $gp0
connect_bd_net [get_bd_pins $ps7/FCLK_CLK0] [get_bd_pins $gp0/aclk]
connect_bd_net [get_bd_pins $reset_fclk/interconnect_aresetn] [get_bd_pins $gp0/aresetn]
connect_bd_intf_net [get_bd_intf_pins $ps7/M_AXI_GP0] [get_bd_intf_pins $gp0/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins $gp0/M00_AXI] [get_bd_intf_pins $dma/S_AXI_LITE]

set gpio_control [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 gpio_control]
set_property -dict [list \
    CONFIG.C_IS_DUAL {1} \
    CONFIG.C_GPIO_WIDTH {32} \
    CONFIG.C_GPIO2_WIDTH {32} \
    CONFIG.C_ALL_OUTPUTS {1} \
    CONFIG.C_ALL_OUTPUTS_2 {1} \
    CONFIG.C_DOUT_DEFAULT {0x00000000} \
    CONFIG.C_DOUT_DEFAULT_2 {0x00000000}] $gpio_control
set gpio_status [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 gpio_status]
set_property -dict [list \
    CONFIG.C_IS_DUAL {1} \
    CONFIG.C_GPIO_WIDTH {32} \
    CONFIG.C_GPIO2_WIDTH {32} \
    CONFIG.C_ALL_INPUTS {1} \
    CONFIG.C_ALL_INPUTS_2 {1}] $gpio_status
set gpio_timestamp [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 gpio_timestamp]
set_property -dict [list \
    CONFIG.C_IS_DUAL {1} \
    CONFIG.C_GPIO_WIDTH {32} \
    CONFIG.C_GPIO2_WIDTH {32} \
    CONFIG.C_ALL_INPUTS {1} \
    CONFIG.C_ALL_INPUTS_2 {1}] $gpio_timestamp
set gpio_adc_clock [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 gpio_adc_clock]
set_property -dict [list \
    CONFIG.C_IS_DUAL {1} \
    CONFIG.C_GPIO_WIDTH {32} \
    CONFIG.C_GPIO2_WIDTH {32} \
    CONFIG.C_ALL_INPUTS {1} \
    CONFIG.C_ALL_INPUTS_2 {1}] $gpio_adc_clock

foreach gpio [list $gpio_control $gpio_status $gpio_timestamp $gpio_adc_clock] {
    connect_bd_net [get_bd_pins $ps7/FCLK_CLK0] [get_bd_pins $gpio/s_axi_aclk]
    connect_bd_net [get_bd_pins $reset_fclk/peripheral_aresetn] [get_bd_pins $gpio/s_axi_aresetn]
}
connect_bd_intf_net [get_bd_intf_pins $gp0/M01_AXI] [get_bd_intf_pins $gpio_control/S_AXI]
connect_bd_intf_net [get_bd_intf_pins $gp0/M02_AXI] [get_bd_intf_pins $gpio_status/S_AXI]
connect_bd_intf_net [get_bd_intf_pins $gp0/M03_AXI] [get_bd_intf_pins $gpio_timestamp/S_AXI]
connect_bd_intf_net [get_bd_intf_pins $gp0/M04_AXI] [get_bd_intf_pins $gpio_adc_clock/S_AXI]

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
create_bd_port -dir I spi_sclk
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

# GPIO control channel 1 keeps the legacy low bits and adds a static test-source
# profile plus three event toggles. Channel 2 is the full 32-bit NCO increment.
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

set test_mode_slice [create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 test_mode_slice]
set_property -dict [list CONFIG.DIN_WIDTH {32} CONFIG.DIN_FROM {4} CONFIG.DIN_TO {3} CONFIG.DOUT_WIDTH {2}] $test_mode_slice
set test_amplitude_slice [create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 test_amplitude_slice]
set_property -dict [list CONFIG.DIN_WIDTH {32} CONFIG.DIN_FROM {19} CONFIG.DIN_TO {8} CONFIG.DOUT_WIDTH {12}] $test_amplitude_slice
for {set bit_index 5} {$bit_index < 8} {incr bit_index} {
    set control_slice($bit_index) [create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 control_slice_$bit_index]
    set_property -dict [list \
        CONFIG.DIN_WIDTH {32} \
        CONFIG.DIN_FROM $bit_index \
        CONFIG.DIN_TO $bit_index \
        CONFIG.DOUT_WIDTH {1}] $control_slice($bit_index)
    connect_bd_net [get_bd_pins $gpio_control/gpio_io_o] [get_bd_pins $control_slice($bit_index)/Din]
}
connect_bd_net [get_bd_pins $gpio_control/gpio_io_o] \
    [get_bd_pins $test_mode_slice/Din] \
    [get_bd_pins $test_amplitude_slice/Din]
connect_bd_net [get_bd_pins $test_mode_slice/Dout] [get_bd_pins $pipeline/test_mode]
connect_bd_net [get_bd_pins $test_amplitude_slice/Dout] [get_bd_pins $pipeline/test_amplitude]
connect_bd_net [get_bd_pins $gpio_control/gpio2_io_o] [get_bd_pins $pipeline/test_phase_increment]
connect_bd_net [get_bd_pins $control_slice(5)/Dout] [get_bd_pins $pipeline/inject_otr_toggle]
connect_bd_net [get_bd_pins $control_slice(6)/Dout] [get_bd_pins $pipeline/inject_overflow_toggle]
connect_bd_net [get_bd_pins $control_slice(7)/Dout] [get_bd_pins $pipeline/inject_frame_drop_toggle]

# Snapshot {adc_tick,frame_timestamp_tick,frame_id,status_word} coherently into
# FCLK0. The frame fields remain unchanged until a new generation is accepted;
# adc_tick supplies a read-only clock anchor before capture is enabled.
set status_concat [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat:2.1 status_concat]
set_property -dict [list \
    CONFIG.NUM_PORTS {4} \
    CONFIG.IN0_WIDTH {32} \
    CONFIG.IN1_WIDTH {32} \
    CONFIG.IN2_WIDTH {64} \
    CONFIG.IN3_WIDTH {64}] $status_concat
connect_bd_net [get_bd_pins $pipeline/status_word] [get_bd_pins $status_concat/In0]
connect_bd_net [get_bd_pins $pipeline/frame_id] [get_bd_pins $status_concat/In1]
connect_bd_net [get_bd_pins $pipeline/frame_timestamp_tick] [get_bd_pins $status_concat/In2]
connect_bd_net [get_bd_pins $pipeline/adc_tick] [get_bd_pins $status_concat/In3]
connect_bd_net [get_bd_pins $status_concat/dout] [get_bd_pins $status_cdc/src_data]

set status_slice [create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 status_slice]
set_property -dict [list CONFIG.DIN_WIDTH {192} CONFIG.DIN_FROM {31} CONFIG.DIN_TO {0} CONFIG.DOUT_WIDTH {32}] $status_slice
set frame_id_slice [create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 frame_id_slice]
set_property -dict [list CONFIG.DIN_WIDTH {192} CONFIG.DIN_FROM {63} CONFIG.DIN_TO {32} CONFIG.DOUT_WIDTH {32}] $frame_id_slice
set timestamp_lo_slice [create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 timestamp_lo_slice]
set_property -dict [list CONFIG.DIN_WIDTH {192} CONFIG.DIN_FROM {95} CONFIG.DIN_TO {64} CONFIG.DOUT_WIDTH {32}] $timestamp_lo_slice
set timestamp_hi_slice [create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 timestamp_hi_slice]
set_property -dict [list CONFIG.DIN_WIDTH {192} CONFIG.DIN_FROM {127} CONFIG.DIN_TO {96} CONFIG.DOUT_WIDTH {32}] $timestamp_hi_slice
set adc_tick_lo_slice [create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 adc_tick_lo_slice]
set_property -dict [list CONFIG.DIN_WIDTH {192} CONFIG.DIN_FROM {159} CONFIG.DIN_TO {128} CONFIG.DOUT_WIDTH {32}] $adc_tick_lo_slice
set adc_tick_hi_slice [create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 adc_tick_hi_slice]
set_property -dict [list CONFIG.DIN_WIDTH {192} CONFIG.DIN_FROM {191} CONFIG.DIN_TO {160} CONFIG.DOUT_WIDTH {32}] $adc_tick_hi_slice
connect_bd_net [get_bd_pins $status_cdc/dst_data] \
    [get_bd_pins $status_slice/Din] \
    [get_bd_pins $frame_id_slice/Din] \
    [get_bd_pins $timestamp_lo_slice/Din] \
    [get_bd_pins $timestamp_hi_slice/Din] \
    [get_bd_pins $adc_tick_lo_slice/Din] \
    [get_bd_pins $adc_tick_hi_slice/Din]
connect_bd_net [get_bd_pins $status_slice/Dout] [get_bd_pins $gpio_status/gpio_io_i]
connect_bd_net [get_bd_pins $frame_id_slice/Dout] [get_bd_pins $gpio_status/gpio2_io_i]
connect_bd_net [get_bd_pins $timestamp_lo_slice/Dout] [get_bd_pins $gpio_timestamp/gpio_io_i]
connect_bd_net [get_bd_pins $timestamp_hi_slice/Dout] [get_bd_pins $gpio_timestamp/gpio2_io_i]
connect_bd_net [get_bd_pins $adc_tick_lo_slice/Dout] [get_bd_pins $gpio_adc_clock/gpio_io_i]
connect_bd_net [get_bd_pins $adc_tick_hi_slice/Dout] [get_bd_pins $gpio_adc_clock/gpio2_io_i]

# Fix software-visible registers before the generic allocator handles DDR.
# This avoids address drift when a new GP0 peripheral changes lexical order.
set ps_data_space [require_one "PS7 GP0 data address space" \
    [get_bd_addr_spaces -quiet $ps7/Data]]
foreach {slave_segment offset} [list \
    $dma/S_AXI_LITE/Reg 0x40400000 \
    $gpio_control/S_AXI/Reg 0x41200000 \
    $gpio_status/S_AXI/Reg 0x41210000 \
    $gpio_timestamp/S_AXI/Reg 0x41220000 \
    $gpio_adc_clock/S_AXI/Reg 0x41230000] {
    set segment [require_one "AXI slave segment $slave_segment" \
        [get_bd_addr_segs -quiet $slave_segment]]
    assign_bd_address -offset $offset -range 0x00010000 \
        -target_address_space $ps_data_space $segment
}
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

# The PS7 customizer can normalize clock settings during target generation.
# Validate the generated handoff, and reject the MIO47 reset experiment which
# made the RTL8211F disappear from MDIO on the current board.
set ps7_parameter_files [glob -nocomplain [file join $project_root \
    cyclescope_system.gen sources_1 bd cyclescope_system ip \
    cyclescope_system_processing_system7_0_0 ps7_parameters.xml]]
if {[llength $ps7_parameter_files] != 1} {
    error "expected one generated ps7_parameters.xml, found: $ps7_parameter_files"
}
set ps7_parameter_file [lindex $ps7_parameter_files 0]
set ps7_channel [open $ps7_parameter_file r]
set ps7_parameters [read $ps7_channel]
close $ps7_channel
foreach expected_line [list \
    {<PARAMETER NAME="PCW_ENET0_PERIPHERAL_CLKSRC" VALUE="IO PLL" />} \
    {<PARAMETER NAME="PCW_ENET0_PERIPHERAL_DIVISOR0" VALUE="8" />} \
    {<PARAMETER NAME="PCW_ENET0_PERIPHERAL_DIVISOR1" VALUE="5" />} \
    {<PARAMETER NAME="PCW_ENET0_PERIPHERAL_FREQMHZ" VALUE="100 Mbps" />}] {
    if {[string first $expected_line $ps7_parameters] < 0} {
        error "generated PS7 Ethernet configuration missing: $expected_line"
    }
}
foreach forbidden_line [list \
    {<PARAMETER NAME="PCW_GPIO_MIO_GPIO_ENABLE" VALUE="1" />} \
    {<PARAMETER NAME="PCW_ENET_RESET_ENABLE" VALUE="1" />} \
    {<PARAMETER NAME="PCW_ENET0_RESET_ENABLE" VALUE="1" />} \
    {<PARAMETER NAME="PCW_ENET0_RESET_IO" VALUE="MIO 47" />}] {
    if {[string first $forbidden_line $ps7_parameters] >= 0} {
        error "generated PS7 Ethernet configuration still enables unsafe reset: $forbidden_line"
    }
}
puts "PS7_ENET0_FIXED_100MBPS_NO_MIO_RESET_PASS"

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
    clk_out2_cyclescope_system_cyclescope_clocking_0_0]]
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

# Keep the synthesis-only handoff stable for software-only rebuilds.
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

if {!$do_implementation} {
    exit
}

if {$do_raw_iob_ila} {
    set raw_data_cells {}
    set raw_probe_nets {}
    for {set bit 0} {$bit < 12} {incr bit} {
        set cell [adc_data_iob_cell $bit]
        set q_pin [cell_pin "ADC data IOB cell $bit" $cell Q]
        set q_net [pin_net "ADC data IOB Q $bit" $q_pin]
        lappend raw_data_cells $cell
        lappend raw_probe_nets $q_net
    }
    set raw_otr_cell [adc_otr_iob_cell]
    set raw_otr_q_pin [cell_pin "ADC OTR IOB cell" $raw_otr_cell Q]
    set raw_otr_probe_net [pin_net "ADC OTR IOB Q" $raw_otr_q_pin]

    set all_probe_nets [concat $raw_probe_nets [list $raw_otr_probe_net]]
    if {[llength [lsort -unique $all_probe_nets]] != 13} {
        error "raw IOB probes are not 13 unique Q nets: $all_probe_nets"
    }

    set adc_clock_pin [cell_pin "ADC data IOB cell 0" \
        [lindex $raw_data_cells 0] C]
    set adc_clock_net [pin_net "ADC IOB clock" $adc_clock_pin]
    foreach cell [concat $raw_data_cells [list $raw_otr_cell]] {
        set cell_clock_net [pin_net "ADC IOB cell clock" \
            [cell_pin "ADC IOB cell" $cell C]]
        if {$cell_clock_net ne $adc_clock_net} {
            error "ADC IOB cells do not share one sample clock: $cell"
        }
    }

    set_property MARK_DEBUG true $all_probe_nets
    set raw_ila_core u_raw_iob_ila
    create_debug_core $raw_ila_core ila
    set raw_ila_object [require_one "raw IOB ILA core" \
        [get_debug_cores -quiet $raw_ila_core]]
    set_property -dict [list \
        ALL_PROBE_SAME_MU true \
        ALL_PROBE_SAME_MU_CNT 1 \
        C_ADV_TRIGGER false \
        C_DATA_DEPTH 16384 \
        C_EN_STRG_QUAL false \
        C_INPUT_PIPE_STAGES 0 \
        C_TRIGIN_EN false \
        C_TRIGOUT_EN false] $raw_ila_object

    connect_debug_port $raw_ila_core/clk $adc_clock_net
    set raw_data_probe [require_one "raw IOB ILA data probe" \
        [get_debug_ports -quiet $raw_ila_core/probe0]]
    set_property PORT_WIDTH 12 $raw_data_probe
    set_property PROBE_TYPE DATA_AND_TRIGGER $raw_data_probe
    connect_debug_port $raw_ila_core/probe0 $raw_probe_nets

    create_debug_port $raw_ila_core probe
    set raw_otr_probe [require_one "raw IOB ILA OTR probe" \
        [get_debug_ports -quiet $raw_ila_core/probe1]]
    set_property PORT_WIDTH 1 $raw_otr_probe
    set_property PROBE_TYPE DATA_AND_TRIGGER $raw_otr_probe
    connect_debug_port $raw_ila_core/probe1 $raw_otr_probe_net

    set raw_probe_ports [get_debug_ports -quiet ${raw_ila_core}/probe*]
    if {[llength $raw_probe_ports] != 2 ||
        [get_property PORT_WIDTH $raw_data_probe] != 12 ||
        [get_property PORT_WIDTH $raw_otr_probe] != 1} {
        error "raw IOB ILA probe definition mismatch: $raw_probe_ports"
    }

    report_debug_core -full_path -file \
        [file join $report_root raw_iob_ila_debug_core.rpt]
    save_constraints -force
    if {![file exists $raw_ila_xdc] || [file size $raw_ila_xdc] == 0} {
        error "raw IOB ILA target XDC was not saved: $raw_ila_xdc"
    }
    puts "RAW_IOB_ILA_PROBE_PASS"
}

close_design
launch_runs impl_1 -to_step write_bitstream -jobs 8
wait_on_run impl_1
set impl_status [get_property STATUS [get_runs impl_1]]
if {![string match "*Complete!*" $impl_status]} {
    error "system implementation failed: $impl_status"
}
open_run impl_1

set impl_report_root [file join $report_root implementation]
file mkdir $impl_report_root
report_utilization -file [file join $impl_report_root utilization.rpt]
report_timing_summary -delay_type min_max -report_unconstrained \
    -check_timing_verbose -file [file join $impl_report_root timing_summary.rpt]
report_drc -file [file join $impl_report_root drc.rpt]
report_methodology -file [file join $impl_report_root methodology.rpt]
report_io -file [file join $impl_report_root io.rpt]
report_clock_utilization -file [file join $impl_report_root clock_utilization.rpt]
report_power -file [file join $impl_report_root power.rpt]

set impl_cdc_clocks [get_clocks [list \
    clk_fpga_0 \
    clk_out2_cyclescope_system_cyclescope_clocking_0_0]]
set impl_cdc_text [report_cdc -details -from $impl_cdc_clocks \
    -to $impl_cdc_clocks -return_string]
set impl_cdc_file [open [file join $impl_report_root cdc.rpt] w]
puts $impl_cdc_file $impl_cdc_text
close $impl_cdc_file
if {[regexp {CDC-[0-9]+[[:space:]]+Critical} $impl_cdc_text]} {
    error "critical post-route CDC finding detected"
}
report_bus_skew -file [file join $impl_report_root bus_skew.rpt]

if {$do_raw_iob_ila} {
    set raw_timing_path [file join $impl_report_root raw_iob_input_timing.rpt]
    set raw_timing_channel [open $raw_timing_path w]
    puts $raw_timing_channel \
        "label port cell loc setup_slack_ns hold_slack_ns q_net"
    set raw_iob_locations {}
    set raw_adc_worst_setup 1.0e9
    set raw_adc_worst_hold 1.0e9

    set raw_iob_records {}
    for {set bit 0} {$bit < 12} {incr bit} {
        set input_port [require_one "ADC input port $bit" [get_ports -quiet \
            -regexp [format {^Adc_In_A\[%d\]$} $bit]]]
        lappend raw_iob_records [list "A[expr {$bit + 1}]" $input_port \
            [adc_data_iob_cell $bit]]
    }
    lappend raw_iob_records [list ORA \
        [require_one "ADC OTR input port" [get_ports -quiet Otr_A]] \
        [adc_otr_iob_cell]]

    foreach record $raw_iob_records {
        lassign $record label input_port cell
        if {![get_property IOB $cell]} {
            error "$label ADC input register lost IOB placement: $cell"
        }
        set location [get_property LOC $cell]
        if {![string match "ILOGIC_*" $location]} {
            error "$label ADC input register is not in ILOGIC: $cell LOC=$location"
        }
        lappend raw_iob_locations $location

        set d_pin [cell_pin "$label ADC input register" $cell D]
        set d_net_segments [get_nets -quiet -segments -of_objects $d_pin]
        if {[llength $d_net_segments] == 0} {
            error "$label ADC input D has no connected hierarchical net segments"
        }
        set load_pins [get_pins -quiet -leaf -of_objects $d_net_segments \
            -filter {DIRECTION == IN}]
        if {[llength $load_pins] != 1 || [lindex $load_pins 0] ne $d_pin} {
            error "$label ADC IBUF output has an unexpected sampling fanout: $load_pins"
        }
        set driver_pin [require_one "$label ADC IBUF driver" [get_pins -quiet \
            -leaf -of_objects $d_net_segments -filter {DIRECTION == OUT}]]
        set driver_cell [require_one "$label ADC IBUF cell" \
            [get_cells -quiet -of_objects $driver_pin]]
        if {![string match "IBUF*" [get_property REF_NAME $driver_cell]]} {
            error "$label ADC input does not drive the IOB register through IBUF only: $driver_cell"
        }

        set setup_path [require_one "$label ADC setup path" [get_timing_paths \
            -quiet -delay_type max -max_paths 1 -from $input_port -to $d_pin]]
        set hold_path [require_one "$label ADC hold path" [get_timing_paths \
            -quiet -delay_type min -max_paths 1 -from $input_port -to $d_pin]]
        set input_setup_slack [get_property SLACK $setup_path]
        set input_hold_slack [get_property SLACK $hold_path]
        if {$input_setup_slack < 1.0 || $input_hold_slack < 1.0} {
            error "$label ADC input timing margin below 1 ns: setup=$input_setup_slack hold=$input_hold_slack"
        }
        if {$input_setup_slack < $raw_adc_worst_setup} {
            set raw_adc_worst_setup $input_setup_slack
        }
        if {$input_hold_slack < $raw_adc_worst_hold} {
            set raw_adc_worst_hold $input_hold_slack
        }

        set q_net [pin_net "$label ADC input Q" \
            [cell_pin "$label ADC input register" $cell Q]]
        if {![get_property MARK_DEBUG $q_net]} {
            error "$label ADC input Q net lost MARK_DEBUG: $q_net"
        }
        puts $raw_timing_channel [join [list $label $input_port $cell $location \
            $input_setup_slack $input_hold_slack $q_net] " "]
    }
    close $raw_timing_channel

    if {[llength [lsort -unique $raw_iob_locations]] != 13} {
        error "ADC inputs do not occupy 13 unique ILOGIC sites: $raw_iob_locations"
    }
    puts "RAW_IOB_ILA_ADC_SETUP_WORST_SLACK_NS=$raw_adc_worst_setup"
    puts "RAW_IOB_ILA_ADC_HOLD_WORST_SLACK_NS=$raw_adc_worst_hold"
    puts "RAW_IOB_ILA_IOB_PASS"
    puts "RAW_IOB_ILA_ADC_TIMING_PASS"

    set raw_ila_object [require_one "implemented raw IOB ILA core" \
        [get_debug_cores -quiet u_raw_iob_ila]]
    set raw_data_probe [require_one "implemented raw IOB ILA data probe" \
        [get_debug_ports -quiet u_raw_iob_ila/probe0]]
    set raw_otr_probe [require_one "implemented raw IOB ILA OTR probe" \
        [get_debug_ports -quiet u_raw_iob_ila/probe1]]
    if {[get_property C_DATA_DEPTH $raw_ila_object] != 16384 ||
        [get_property PORT_WIDTH $raw_data_probe] != 12 ||
        [get_property PORT_WIDTH $raw_otr_probe] != 1} {
        error "implemented raw IOB ILA definition mismatch"
    }
    report_debug_core -full_path -file \
        [file join $impl_report_root raw_iob_ila_debug_core.rpt]
}

set setup_path [get_timing_paths -delay_type max -max_paths 1]
set hold_path [get_timing_paths -delay_type min -max_paths 1]
if {[llength $setup_path] == 0 || [llength $hold_path] == 0} {
    error "post-route setup/hold path missing"
}
set setup_slack [get_property SLACK [lindex $setup_path 0]]
set hold_slack [get_property SLACK [lindex $hold_path 0]]
puts "SYSTEM_IMPL_SETUP_WORST_SLACK_NS=$setup_slack"
puts "SYSTEM_IMPL_HOLD_WORST_SLACK_NS=$hold_slack"
if {$setup_slack < 0.0 || $hold_slack < 0.0} {
    error "post-route timing failed: setup=$setup_slack hold=$hold_slack ns"
}

set blocking_drc {}
foreach violation [get_drc_violations -quiet] {
    set severity [get_property SEVERITY $violation]
    if {$severity eq "Error" || $severity eq "Critical Warning"} {
        lappend blocking_drc $violation
    }
}
if {[llength $blocking_drc] != 0} {
    error "blocking post-route DRC findings: $blocking_drc"
}
if {$do_raw_iob_ila} {
    puts "RAW_IOB_ILA_DRC_CDC_PASS"
}

set checkpoint_name [expr {$do_raw_iob_ila ?
    "cyclescope_system_raw_iob_ila_p${adc_sample_phase}_impl.dcp" :
    "cyclescope_system_impl.dcp"}]
write_checkpoint -force [file join $build_root $checkpoint_name]
set generated_bit [file join $project_root cyclescope_system.runs impl_1 \
    cyclescope_system_wrapper.bit]
if {![file exists $generated_bit] || [file size $generated_bit] == 0} {
    error "implementation completed without a non-empty bitstream"
}
if {$do_raw_iob_ila} {
    set artifact_stem cyclescope_system_raw_iob_ila_p${adc_sample_phase}
    set exported_bit [file join $hardware_root ${artifact_stem}.bit]
    set exported_ltx [file join $hardware_root ${artifact_stem}.ltx]
    set manifest_path [file join $hardware_root ${artifact_stem}.manifest.json]
    set sha256_path [file join $hardware_root ${artifact_stem}.sha256]
    file copy -force $generated_bit $exported_bit
    write_debug_probes -force $exported_ltx
    foreach artifact [list $exported_bit $exported_ltx] {
        if {![file exists $artifact] || [file size $artifact] == 0} {
            error "raw IOB ILA artifact missing or empty: $artifact"
        }
    }

    set bit_sha256 [lindex [split [string trim \
        [exec sha256sum -- $exported_bit]]] 0]
    set ltx_sha256 [lindex [split [string trim \
        [exec sha256sum -- $exported_ltx]]] 0]
    if {[string length $bit_sha256] != 64 || [string length $ltx_sha256] != 64} {
        error "raw IOB ILA SHA-256 generation failed"
    }

    set manifest_channel [open $manifest_path w]
    puts $manifest_channel "\{"
    puts $manifest_channel \
        "  \"format\": \"CycleScope raw IOB ILA build manifest v1\","
    puts $manifest_channel "  \"sample_rate_hz\": 65000000,"
    puts $manifest_channel "  \"sample_phase_deg\": $adc_sample_phase,"
    puts $manifest_channel "  \"capture_depth\": 16384,"
    puts $manifest_channel "  \"bitstream\": \{"
    puts $manifest_channel \
        "    \"file\": \"[file tail $exported_bit]\","
    puts $manifest_channel "    \"sha256\": \"$bit_sha256\""
    puts $manifest_channel "  \},"
    puts $manifest_channel "  \"debug_probes\": \{"
    puts $manifest_channel \
        "    \"file\": \"[file tail $exported_ltx]\","
    puts $manifest_channel "    \"sha256\": \"$ltx_sha256\""
    puts $manifest_channel "  \},"
    puts $manifest_channel {  "probe_mapping": [}
    for {set bit 0} {$bit < 12} {incr bit} {
        set separator [expr {$bit == 11 ? "" : ","}]
        puts $manifest_channel [format \
            {    {"probe": "probe0[%d]", "fpga_port": "Adc_In_A[%d]", "module_signal": "A%d", "expected_adc_bit": "D%d"}%s} \
            $bit $bit [expr {$bit + 1}] $bit $separator]
    }
    puts $manifest_channel {  ],}
    puts $manifest_channel \
        {  "otr_probe": {"probe": "probe1[0]", "fpga_port": "Otr_A", "module_signal": "ORA"}}
    puts $manifest_channel "\}"
    close $manifest_channel

    set sha256_channel [open $sha256_path w]
    puts $sha256_channel "$bit_sha256  [file tail $exported_bit]"
    puts $sha256_channel "$ltx_sha256  [file tail $exported_ltx]"
    close $sha256_channel
    foreach artifact [list $manifest_path $sha256_path] {
        if {![file exists $artifact] || [file size $artifact] == 0} {
            error "raw IOB ILA metadata missing or empty: $artifact"
        }
    }

    puts "RAW_IOB_ILA_BITSTREAM=$exported_bit"
    puts "RAW_IOB_ILA_LTX=$exported_ltx"
    puts "RAW_IOB_ILA_MANIFEST=$manifest_path"
    puts "RAW_IOB_ILA_BITSTREAM_SHA256=$bit_sha256"
    puts "RAW_IOB_ILA_LTX_SHA256=$ltx_sha256"
    puts "RAW_IOB_ILA_SHA256_PASS"
    puts "RAW_IOB_ILA_IMPLEMENTATION_PASS"
} else {
    set exported_bit [file join $hardware_root cyclescope_system.bit]
    file copy -force $generated_bit $exported_bit
    set bitstream_xsa [file join $hardware_root cyclescope_system_with_bitstream.xsa]
    write_hw_platform -fixed -include_bit -force -file $bitstream_xsa

    puts "SYSTEM_BITSTREAM=$exported_bit"
    puts "SYSTEM_BITSTREAM_XSA=$bitstream_xsa"
    puts "SYSTEM_IMPLEMENTATION_PASS"
}
exit
