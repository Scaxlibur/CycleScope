# CycleScope volatile JTAG loader for XSDB 2025.1.
#
# Safety contract:
#   * input artifacts are validated before any hardware connection;
#   * the default mode is a dry run;
#   * --execute is required before reset, FPGA configuration, or ELF download;
#   * the Digilent cable serial and all three Zynq targets must match uniquely.

proc usage {defaults} {
    puts {Usage:
  xsdb -no-ini scripts/jtag_download.tcl [options]

Default mode validates all artifacts and prints the exact operation plan. It
does not connect to hw_server or touch the board. Add --execute explicitly to
perform the volatile JTAG load.

Options:
  --bit PATH           Implemented PL bitstream.
  --ps7-init PATH      XSA-generated ps7_init.tcl.
  --elf PATH           Vitis application ELF.
  --hw-url URL         Existing hw_server URL.
  --cable-serial ID    Exact Digilent cable serial number.
  --execute            Reset and download after validation.
  -h, --help           Show this help and exit.
}
    puts "Defaults:"
    puts "  --bit [dict get $defaults bit]"
    puts "  --ps7-init [dict get $defaults ps7_init]"
    puts "  --elf [dict get $defaults elf]"
    puts "  --hw-url [dict get $defaults hw_url]"
    puts "  --cable-serial [dict get $defaults cable_serial]"
}

proc parse_arguments {argv defaults} {
    set options $defaults
    dict set options execute 0
    dict set options help 0

    for {set index 0} {$index < [llength $argv]} {incr index} {
        set argument [lindex $argv $index]
        switch -- $argument {
            -h -
            --help {
                dict set options help 1
            }
            --execute {
                dict set options execute 1
            }
            --bit -
            --ps7-init -
            --elf -
            --hw-url -
            --cable-serial {
                incr index
                if {$index >= [llength $argv]} {
                    error "$argument requires a value"
                }
                set value [lindex $argv $index]
                if {$value eq ""} {
                    error "$argument cannot be empty"
                }
                switch -- $argument {
                    --bit { dict set options bit $value }
                    --ps7-init { dict set options ps7_init $value }
                    --elf { dict set options elf $value }
                    --hw-url { dict set options hw_url $value }
                    --cable-serial { dict set options cable_serial $value }
                }
            }
            default {
                error "unknown argument: $argument (use --help)"
            }
        }
    }
    return $options
}

proc require_input_file {label path expected_extension} {
    set normalized [file normalize $path]
    if {![file exists $normalized]} {
        error "$label missing: $normalized"
    }
    if {![file isfile $normalized]} {
        error "$label is not a regular file: $normalized"
    }
    if {![file readable $normalized]} {
        error "$label is not readable: $normalized"
    }
    if {[file size $normalized] == 0} {
        error "$label is empty: $normalized"
    }
    if {[string tolower [file extension $normalized]] ne $expected_extension} {
        error "$label must use the $expected_extension extension: $normalized"
    }
    return $normalized
}

proc validate_ps7_init {path} {
    set channel [open $path r]
    try {
        set contents [read $channel]
    } finally {
        close $channel
    }
    foreach command {ps7_init ps7_post_config} {
        if {[string first "proc $command " $contents] < 0} {
            error "XSA init script does not define $command: $path"
        }
    }
}

proc validate_elf {path} {
    set channel [open $path rb]
    try {
        set magic [read $channel 4]
    } finally {
        close $channel
    }
    binary scan $magic H* magic_hex
    if {$magic_hex ne "7f454c46"} {
        error "application file does not have ELF magic: $path"
    }
}

proc validate_worktree {fpga_root} {
    if {[catch {
        set git_root [string trim [exec git -C $fpga_root rev-parse --show-toplevel]]
        set branch [string trim [exec git -C $fpga_root branch --show-current]]
    } git_error]} {
        error "cannot validate FPGA worktree: $git_error"
    }
    if {[file normalize $git_root] ne [file normalize $fpga_root]} {
        error "unexpected Git worktree root: $git_root"
    }
    if {$branch ne "main"} {
        error "refusing JTAG operation on branch $branch; expected main"
    }
}

proc describe_target {properties} {
    set name "<unnamed>"
    set serial "<no-serial>"
    if {[dict exists $properties name]} {
        set name [dict get $properties name]
    }
    if {[dict exists $properties jtag_cable_serial]} {
        set serial [dict get $properties jtag_cable_serial]
    }
    return "$name@$serial"
}

proc target_is_digilent {properties} {
    foreach key {jtag_cable_name jtag_cable_manufacturer jtag_cable_product} {
        if {[dict exists $properties $key] &&
            [string match -nocase "*Digilent*" [dict get $properties $key]]} {
            return 1
        }
    }
    return 0
}

proc select_unique_digilent_target {cable_serial name_pattern description} {
    set deadline [expr {[clock milliseconds] + 10000}]
    set last_seen {}
    while {1} {
        set matches {}
        if {![catch {set candidates [targets -target-properties]} query_error]} {
            set last_seen {}
            foreach properties $candidates {
                lappend last_seen [describe_target $properties]
                if {![dict exists $properties target_id] ||
                    ![dict exists $properties name] ||
                    ![dict exists $properties jtag_cable_serial]} {
                    continue
                }
                if {[dict get $properties jtag_cable_serial] ne $cable_serial} {
                    continue
                }
                if {![string match -nocase $name_pattern [dict get $properties name]]} {
                    continue
                }
                if {![target_is_digilent $properties]} {
                    error "$description matched serial $cable_serial but is not a Digilent target"
                }
                lappend matches $properties
            }
            if {[llength $matches] == 1} {
                set selected [lindex $matches 0]
                targets [dict get $selected target_id]
                puts "JTAG_TARGET_[string toupper $description]=[describe_target $selected]"
                return
            }
            if {[llength $matches] > 1} {
                set duplicate_names {}
                foreach properties $matches {
                    lappend duplicate_names [describe_target $properties]
                }
                error "multiple $description targets matched: $duplicate_names"
            }
        }
        if {[clock milliseconds] >= $deadline} {
            if {[info exists query_error] && $last_seen eq {}} {
                error "cannot enumerate $description target: $query_error"
            }
            error "no $description target matched Digilent serial $cable_serial; seen: $last_seen"
        }
        after 250
    }
}

proc print_plan {options} {
    puts "CYCLESCOPE_JTAG_MODE=[expr {[dict get $options execute] ? "EXECUTE" : "DRY_RUN"}]"
    puts "JTAG_HW_URL=[dict get $options hw_url]"
    puts "JTAG_CABLE_SERIAL=[dict get $options cable_serial]"
    puts "JTAG_BIT=[dict get $options bit]"
    puts "JTAG_PS7_INIT=[dict get $options ps7_init]"
    puts "JTAG_ELF=[dict get $options elf]"
    puts {JTAG_SEQUENCE=connect -> select APU -> system reset/stop -> configure xc7z010 -> ps7_init -> ps7_post_config -> download A9#0 ELF -> continue}
}

proc execute_download {options} {
    set cable_serial [dict get $options cable_serial]
    set connection ""
    set saved_force_mem ""
    try {
        puts "JTAG_STEP=connect"
        set connection [connect -url [dict get $options hw_url]]

        select_unique_digilent_target $cable_serial "APU*" apu
        puts "JTAG_STEP=system_reset"
        rst -system -stop
        after 1000

        select_unique_digilent_target $cable_serial "xc7z010*" fpga
        puts "JTAG_STEP=configure_fpga"
        fpga -file [dict get $options bit]

        select_unique_digilent_target $cable_serial "APU*" apu
        set saved_force_mem [configparams force-mem-accesses]
        configparams force-mem-accesses 1
        puts "JTAG_STEP=source_ps7_init"
        # Vitis emits global variables that are referenced by global helper
        # procedures. Source the file at Tcl level 0 even though this loader
        # runs inside a procedure, otherwise those variables become locals.
        uplevel #0 [list source [dict get $options ps7_init]]
        foreach command {ps7_init ps7_post_config} {
            if {[llength [info commands $command]] != 1} {
                error "sourced init script does not provide $command"
            }
        }
        puts "JTAG_STEP=ps7_init"
        ps7_init
        puts "JTAG_STEP=ps7_post_config"
        ps7_post_config

        select_unique_digilent_target $cable_serial \
            "ARM Cortex-A9 MPCore #0" a9_core0
        puts "JTAG_STEP=download_elf"
        dow -clear [dict get $options elf]
        puts "JTAG_STEP=continue"
        con
        puts "CYCLESCOPE_JTAG_DOWNLOAD_PASS"
    } finally {
        if {$saved_force_mem ne ""} {
            catch {configparams force-mem-accesses $saved_force_mem}
        }
        if {$connection ne ""} {
            catch {disconnect $connection}
        }
    }
}

proc main {argv} {
    set script_dir [file dirname [file normalize [info script]]]
    set app_root [file normalize [file join $script_dir ..]]
    set fpga_root [file normalize [file join $app_root .. ..]]
    set defaults [dict create \
        bit [file join $fpga_root Zynq_7010_PL build system hardware \
            cyclescope_system.bit] \
        ps7_init [file join $app_root build vitis workspace cyclescope_platform \
            export cyclescope_platform hw sdt ps7_init.tcl] \
        elf [file join $app_root build vitis workspace cyclescope_cslp_app build \
            cyclescope_cslp_app.elf] \
        hw_url tcp:127.0.0.1:3121 \
        cable_serial 210241398254]

    validate_worktree $fpga_root

    set options [parse_arguments $argv $defaults]
    if {[dict get $options help]} {
        usage $defaults
        return
    }

    set xsdb_version [version]
    if {![string match "2025.1*" $xsdb_version]} {
        error "XSDB 2025.1 required, found: $xsdb_version"
    }

    dict set options bit [require_input_file \
        bitstream [dict get $options bit] .bit]
    dict set options ps7_init [require_input_file \
        ps7_init.tcl [dict get $options ps7_init] .tcl]
    dict set options elf [require_input_file \
        application_ELF [dict get $options elf] .elf]
    validate_ps7_init [dict get $options ps7_init]
    validate_elf [dict get $options elf]

    print_plan $options
    if {![dict get $options execute]} {
        puts "CYCLESCOPE_JTAG_DRY_RUN_PASS"
        puts "Hardware was not connected or modified; add --execute explicitly."
        return
    }
    execute_download $options
}

if {[catch {main $argv} result options]} {
    puts stderr "CYCLESCOPE_JTAG_ERROR=$result"
    exit 1
}
exit 0
