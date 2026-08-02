set script_dir [file dirname [file normalize [info script]]]
set pl_root [file normalize [file join $script_dir ..]]
set build_root [file join $pl_root build synth_core]
set report_root [file join $build_root reports]

file delete -force $build_root
file mkdir $report_root

create_project -in_memory -part xc7z010clg400-1
read_verilog -sv [list \
    [file join $pl_root rtl fir_coeffs_pkg.sv] \
    [file join $pl_root rtl ad9226_frontend.sv] \
    [file join $pl_root rtl test_pattern_generator.sv] \
    [file join $pl_root rtl fir_mac_decimator.sv] \
    [file join $pl_root rtl fir_decimator_16.sv] \
    [file join $pl_root rtl frame_ram.sv] \
    [file join $pl_root rtl frame_store_axis_spi.sv] \
    [file join $pl_root rtl cyclescope_pipeline.sv]]
read_xdc [file join $pl_root constraints pipeline_timing.xdc]
synth_design -top cyclescope_pipeline -mode out_of_context -part xc7z010clg400-1

report_utilization -file [file join $report_root utilization.rpt]
report_timing_summary -delay_type min_max -report_unconstrained -check_timing_verbose \
    -file [file join $report_root timing_summary.rpt]
report_methodology -file [file join $report_root methodology.rpt]
write_checkpoint -force [file join $build_root cyclescope_pipeline_synth.dcp]

set timing_paths [get_timing_paths -delay_type max -max_paths 1]
if {[llength $timing_paths] == 0} {
    error "no timing path found after core synthesis"
}
set worst_slack [get_property SLACK [lindex $timing_paths 0]]
set dsp_count [llength [get_cells -hier -filter {REF_NAME =~ DSP48*}]]
set bram36_count [llength [get_cells -hier -filter {REF_NAME =~ RAMB36*}]]
set bram18_count [llength [get_cells -hier -filter {REF_NAME =~ RAMB18*}]]
puts "CORE_SYNTH_WORST_SLACK_NS=$worst_slack"
puts "CORE_SYNTH_DSP=$dsp_count BRAM36=$bram36_count BRAM18=$bram18_count"
if {$worst_slack < 0.0} {
    error "core synthesis timing failed: slack=$worst_slack ns"
}
if {$dsp_count > 48} {
    error "core DSP budget exceeded: $dsp_count > 48"
}

puts "CORE_SYNTH_PASS"
exit
