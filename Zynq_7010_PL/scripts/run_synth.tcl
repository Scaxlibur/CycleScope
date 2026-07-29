set script_dir [file dirname [file normalize [info script]]]
set pl_root [file normalize [file join $script_dir ..]]
set build_root [file join $pl_root build synth_filter]
set report_root [file join $build_root reports]

file delete -force $build_root
file mkdir $report_root

create_project -in_memory -part xc7z010clg400-1
read_verilog -sv [list \
    [file join $pl_root rtl fir_coeffs_pkg.sv] \
    [file join $pl_root rtl fir_mac_decimator.sv] \
    [file join $pl_root rtl fir_decimator_16.sv]]
read_xdc [file join $pl_root constraints core_timing.xdc]
synth_design -top fir_decimator_16 -mode out_of_context -part xc7z010clg400-1

report_utilization -file [file join $report_root utilization.rpt]
report_timing_summary -delay_type min_max -report_unconstrained -check_timing_verbose \
    -file [file join $report_root timing_summary.rpt]
report_methodology -file [file join $report_root methodology.rpt]
write_checkpoint -force [file join $build_root fir_decimator_16_synth.dcp]

set timing_paths [get_timing_paths -delay_type max -max_paths 1]
if {[llength $timing_paths] == 0} {
    error "no timing path found after synthesis"
}
set worst_slack [get_property SLACK [lindex $timing_paths 0]]
puts "FILTER_SYNTH_WORST_SLACK_NS=$worst_slack"
if {$worst_slack < 0.0} {
    error "filter synthesis timing failed: slack=$worst_slack ns"
}

puts "FILTER_SYNTH_PASS"
exit
