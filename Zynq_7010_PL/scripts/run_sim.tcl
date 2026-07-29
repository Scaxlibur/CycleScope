set script_dir [file dirname [file normalize [info script]]]
set pl_root [file normalize [file join $script_dir ..]]
set build_root [file join $pl_root build sim]

file delete -force $build_root
file mkdir $build_root

set rtl_sources [list \
    [file join $pl_root rtl fir_coeffs_pkg.sv] \
    [file join $pl_root rtl ad9226_frontend.sv] \
    [file join $pl_root rtl fir_mac_decimator.sv] \
    [file join $pl_root rtl fir_decimator_16.sv] \
    [file join $pl_root rtl frame_ram.sv] \
    [file join $pl_root rtl frame_store_axis_spi.sv] \
    [file join $pl_root rtl status_snapshot_cdc.sv] \
    [file join $pl_root rtl cyclescope_pipeline.sv]]

proc run_test {build_root name sources testbench} {
    set test_dir [file join $build_root $name]
    file mkdir $test_dir
    set old_dir [pwd]
    cd $test_dir
    puts "=== XSIM $name ==="
    if {[catch {exec xvlog --sv {*}$sources $testbench 2>@1} output]} {
        puts $output
        error "xvlog failed for $name"
    }
    puts $output
    if {[catch {exec xelab $name -s ${name}_snapshot 2>@1} output]} {
        puts $output
        error "xelab failed for $name"
    }
    puts $output
    if {[catch {exec xsim ${name}_snapshot -runall 2>@1} output]} {
        puts $output
        error "xsim failed for $name"
    }
    puts $output
    if {[string first "TEST_PASS $name" $output] < 0} {
        error "$name did not emit its pass marker"
    }
    cd $old_dir
}

run_test $build_root tb_ad9226_frontend $rtl_sources \
    [file join $pl_root sim tb_ad9226_frontend.sv]
run_test $build_root tb_fir_decimator_16 $rtl_sources \
    [file join $pl_root sim tb_fir_decimator_16.sv]
run_test $build_root tb_frame_store_axis_spi $rtl_sources \
    [file join $pl_root sim tb_frame_store_axis_spi.sv]
run_test $build_root tb_status_snapshot_cdc $rtl_sources \
    [file join $pl_root sim tb_status_snapshot_cdc.sv]

puts "ALL_SIM_TESTS_PASS"
exit
