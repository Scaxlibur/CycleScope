# CycleScope raw-IOB ILA capture for Vivado Hardware Manager 2025.1.
#
# The default mode is a dry run: it validates the build binding and output
# destination without connecting to hardware. --execute is required to arm and
# trigger the ILA. This script never programs the FPGA, resets the PS, downloads
# an ELF, writes QSPI, or accesses MIO47; use the separately audited volatile
# JTAG loader first with the exact bitstream printed by this script.

proc usage {defaults} {
    puts {Usage:
  vivado -mode batch -source scripts/capture_adc_raw_ila.tcl -tclargs [options]

Options:
  --bit PATH           Already-loaded raw-IOB ILA bitstream.
  --ltx PATH           Matching debug-probes file.
  --manifest PATH      Matching build manifest.
  --output-dir PATH    New directory for CSV/native capture and hashes.
  --hw-url URL         Existing Vivado hw_server URL.
  --cable-serial ID    Exact Digilent cable serial.
  --execute            Arm, force-trigger, and upload one capture.
  -h, --help           Show this help.
}
    puts "Defaults:"
    foreach key {bit ltx manifest hw_url cable_serial} {
        puts "  --[string map {_ -} $key] [dict get $defaults $key]"
    }
    puts {  --output-dir <required>}
}

proc parse_arguments {arguments defaults} {
    set options $defaults
    dict set options execute 0
    dict set options help 0
    dict set options output_dir ""

    for {set index 0} {$index < [llength $arguments]} {incr index} {
        set argument [lindex $arguments $index]
        switch -- $argument {
            -h -
            --help {
                dict set options help 1
            }
            --execute {
                dict set options execute 1
            }
            --bit -
            --ltx -
            --manifest -
            --output-dir -
            --hw-url -
            --cable-serial {
                incr index
                if {$index >= [llength $arguments]} {
                    error "$argument requires a value"
                }
                set value [lindex $arguments $index]
                if {$value eq ""} {
                    error "$argument cannot be empty"
                }
                switch -- $argument {
                    --bit { dict set options bit $value }
                    --ltx { dict set options ltx $value }
                    --manifest { dict set options manifest $value }
                    --output-dir { dict set options output_dir $value }
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

proc require_one {label objects} {
    if {[llength $objects] != 1} {
        error "$label expected exactly one object, got [llength $objects]: $objects"
    }
    return [lindex $objects 0]
}

proc require_input_file {label path expected_extension} {
    set normalized [file normalize $path]
    if {![file isfile $normalized] || ![file readable $normalized] ||
        [file size $normalized] == 0} {
        error "$label must be a readable non-empty file: $normalized"
    }
    if {[string tolower [file extension $normalized]] ne $expected_extension} {
        error "$label must use $expected_extension: $normalized"
    }
    return $normalized
}

proc file_sha256 {path} {
    set digest [lindex [split [string trim [exec sha256sum -- $path]]] 0]
    if {![regexp {^[0-9a-f]{64}$} $digest]} {
        error "invalid SHA-256 result for $path: $digest"
    }
    return $digest
}

proc read_text {path} {
    set channel [open $path r]
    try {
        return [read $channel]
    } finally {
        close $channel
    }
}

proc validate_worktree {fpga_root} {
    set git_root [string trim [exec git -C $fpga_root rev-parse --show-toplevel]]
    set branch [string trim [exec git -C $fpga_root branch --show-current]]
    if {[file normalize $git_root] ne [file normalize $fpga_root]} {
        error "unexpected Git worktree root: $git_root"
    }
    if {$branch ne "main"} {
        error "refusing ILA capture on branch $branch; expected main"
    }
}

proc validate_build_binding {options} {
    set manifest_text [read_text [dict get $options manifest]]
    set bit_sha256 [file_sha256 [dict get $options bit]]
    set ltx_sha256 [file_sha256 [dict get $options ltx]]
    foreach binding [list \
        [list bitstream [file tail [dict get $options bit]] $bit_sha256] \
        [list debug_probes [file tail [dict get $options ltx]] $ltx_sha256]] {
        lassign $binding label filename digest
        if {[string first "\"file\": \"$filename\"" $manifest_text] < 0 ||
            [string first "\"sha256\": \"$digest\"" $manifest_text] < 0} {
            error "$label does not match build manifest: $filename $digest"
        }
    }
    foreach {field expected} {
        sample_rate_hz 65000000
        capture_depth 16384
    } {
        set field_pattern [format \
            {"%s"[[:space:]]*:[[:space:]]*(%s)} $field $expected]
        if {![regexp $field_pattern $manifest_text]} {
            error "build manifest $field is not $expected"
        }
    }
    if {![regexp {"sample_phase_deg"[[:space:]]*:[[:space:]]*(0|30|60|90|120|150|180|210|240|270|300|330|345|348|351|354)[[:space:]]*[,\}]} \
        $manifest_text _ sample_phase]} {
        error "build manifest sample phase is not in the reviewed full-cycle diagnostic set"
    }
    dict set options bit_sha256 $bit_sha256
    dict set options ltx_sha256 $ltx_sha256
    dict set options sample_phase $sample_phase
    return $options
}

proc prepare_output_directory {path} {
    if {$path eq ""} {
        error "--output-dir is required"
    }
    set normalized [file normalize $path]
    if {[file exists $normalized]} {
        error "capture output directory already exists: $normalized"
    }
    set parent [file dirname $normalized]
    if {![file isdirectory $parent] || ![file writable $parent]} {
        error "capture output parent is not a writable directory: $parent"
    }
    return $normalized
}

proc select_target {cable_serial} {
    set matches {}
    foreach target [get_hw_targets -quiet] {
        set name [get_property NAME $target]
        if {[string match "*$cable_serial*" $name] &&
            [string match -nocase "*Digilent*" $name]} {
            lappend matches $target
        }
    }
    return [require_one "Digilent hardware target $cable_serial" $matches]
}

proc select_zynq_device {} {
    set matches {}
    foreach device [get_hw_devices -quiet] {
        set description "[get_property NAME $device] [get_property PART $device]"
        if {[string match -nocase "*xc7z010*" $description]} {
            lappend matches $device
        }
    }
    return [require_one "xc7z010 hardware device" $matches]
}

proc execute_capture {options output_dir} {
    set server_open 0
    set target_open 0
    try {
        open_hw_manager
        connect_hw_server -url [dict get $options hw_url]
        set server_open 1
        set target [select_target [dict get $options cable_serial]]
        current_hw_target $target
        open_hw_target $target
        set target_open 1

        set device [select_zynq_device]
        current_hw_device $device
        set_property PROBES.FILE [dict get $options ltx] $device
        set_property FULL_PROBES.FILE [dict get $options ltx] $device
        refresh_hw_device $device

        set ila_matches {}
        foreach ila [get_hw_ilas -quiet -of_objects $device] {
            set description "[get_property NAME $ila] [get_property CELL_NAME $ila]"
            if {[string match "*u_raw_iob_ila*" $description]} {
                lappend ila_matches $ila
            }
        }
        set ila [require_one "u_raw_iob_ila hardware core" $ila_matches]
        set_property CONTROL.TRIGGER_POSITION 8192 $ila

        file mkdir $output_dir
        puts "RAW_IOB_ILA_CAPTURE_STEP=arm"
        run_hw_ila $ila
        after 100
        puts "RAW_IOB_ILA_CAPTURE_STEP=force_trigger"
        run_hw_ila -trigger_now $ila
        wait_on_hw_ila $ila
        puts "RAW_IOB_ILA_CAPTURE_STEP=upload"
        upload_hw_ila_data $ila
        set ila_data [require_one "uploaded raw IOB ILA data" \
            [get_hw_ila_data -quiet -of_objects $ila]]

        set csv_path [file join $output_dir adc_raw_iob.csv]
        set native_path [file join $output_dir adc_raw_iob.ila]
        write_hw_ila_data -force -csv_file $csv_path $ila_data
        write_hw_ila_data -force $native_path $ila_data
        foreach artifact [list $csv_path $native_path] {
            if {![file exists $artifact] || [file size $artifact] == 0} {
                error "ILA capture artifact missing or empty: $artifact"
            }
        }

        set csv_sha256 [file_sha256 $csv_path]
        set native_sha256 [file_sha256 $native_path]
        set metadata_path [file join $output_dir capture_manifest.json]
        set metadata_channel [open $metadata_path w]
        puts $metadata_channel "\{"
        puts $metadata_channel \
            {  "format": "CycleScope raw IOB ILA capture manifest v1",}
        puts $metadata_channel \
            "  \"captured_at_utc\": \"[clock format [clock seconds] -gmt 1 -format {%Y-%m-%dT%H:%M:%SZ}]\","
        puts $metadata_channel "  \"sample_rate_hz\": 65000000,"
        puts $metadata_channel \
            "  \"sample_phase_deg\": [dict get $options sample_phase],"
        puts $metadata_channel "  \"capture_depth\": 16384,"
        puts $metadata_channel \
            "  \"bitstream_sha256\": \"[dict get $options bit_sha256]\","
        puts $metadata_channel \
            "  \"ltx_sha256\": \"[dict get $options ltx_sha256]\","
        puts $metadata_channel \
            "  \"csv\": \{\"file\": \"[file tail $csv_path]\", \"sha256\": \"$csv_sha256\"\},"
        puts $metadata_channel \
            "  \"native\": \{\"file\": \"[file tail $native_path]\", \"sha256\": \"$native_sha256\"\}"
        puts $metadata_channel "\}"
        close $metadata_channel
        if {[file size $metadata_path] == 0} {
            error "ILA capture manifest is empty"
        }
        puts "RAW_IOB_ILA_CAPTURE_CSV=$csv_path"
        puts "RAW_IOB_ILA_CAPTURE_NATIVE=$native_path"
        puts "RAW_IOB_ILA_CAPTURE_MANIFEST=$metadata_path"
        puts "RAW_IOB_ILA_CAPTURE_PASS"
    } finally {
        if {$target_open} {
            catch {close_hw_target}
        }
        if {$server_open} {
            catch {disconnect_hw_server}
        }
        catch {close_hw_manager}
    }
}

proc main {arguments} {
    set script_dir [file dirname [file normalize [info script]]]
    set pl_root [file normalize [file join $script_dir ..]]
    set fpga_root [file normalize [file join $pl_root ..]]
    set build_root [file join $pl_root build diagnostic raw-iob-ila-p300 hardware]
    set stem cyclescope_system_raw_iob_ila_p300
    set defaults [dict create \
        bit [file join $build_root ${stem}.bit] \
        ltx [file join $build_root ${stem}.ltx] \
        manifest [file join $build_root ${stem}.manifest.json] \
        hw_url localhost:3121 \
        cable_serial 210241398254]

    validate_worktree $fpga_root
    set options [parse_arguments $arguments $defaults]
    if {[dict get $options help]} {
        usage $defaults
        return
    }
    if {![string match "2025.1*" [version -short]]} {
        error "Vivado 2025.1 required, found [version -short]"
    }
    dict set options bit [require_input_file bitstream \
        [dict get $options bit] .bit]
    dict set options ltx [require_input_file debug_probes \
        [dict get $options ltx] .ltx]
    dict set options manifest [require_input_file build_manifest \
        [dict get $options manifest] .json]
    set options [validate_build_binding $options]
    set output_dir [prepare_output_directory [dict get $options output_dir]]

    puts "RAW_IOB_ILA_CAPTURE_MODE=[expr {[dict get $options execute] ? "EXECUTE" : "DRY_RUN"}]"
    puts "RAW_IOB_ILA_CAPTURE_BIT=[dict get $options bit]"
    puts "RAW_IOB_ILA_CAPTURE_LTX=[dict get $options ltx]"
    puts "RAW_IOB_ILA_CAPTURE_PHASE_DEG=[dict get $options sample_phase]"
    puts "RAW_IOB_ILA_CAPTURE_OUTPUT=$output_dir"
    puts {RAW_IOB_ILA_CAPTURE_BOUNDARY=arm/force-trigger/upload only; no FPGA program, reset, ELF, QSPI, MIO47, or instrument I/O}
    if {![dict get $options execute]} {
        puts "RAW_IOB_ILA_CAPTURE_DRY_RUN_PASS"
        return
    }
    execute_capture $options $output_dir
}

if {[catch {main $argv} result options]} {
    puts stderr "RAW_IOB_ILA_CAPTURE_ERROR=$result"
    if {[dict exists $options -errorinfo]} {
        puts stderr [dict get $options -errorinfo]
    }
    exit 1
}
