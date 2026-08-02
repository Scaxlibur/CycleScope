# CycleScope M11-J read-only Zynq GEM register snapshot.
#
# This script never resets, stops, downloads, resumes, or writes a target.  It
# only selects the unique Digilent-backed APU and reads fixed SLCR/GEM MMIO
# registers while the application continues running.

set hw_url "tcp:127.0.0.1:3121"
for {set index 0} {$index < [llength $argv]} {incr index} {
    set argument [lindex $argv $index]
    switch -- $argument {
        --hw-url {
            incr index
            if {$index >= [llength $argv]} {
                error "--hw-url requires a value"
            }
            set hw_url [lindex $argv $index]
        }
        default {
            error "unknown argument: $argument"
        }
    }
}

set connection ""
try {
    set connection [connect -url $hw_url]
    set deadline [expr {[clock milliseconds] + 10000}]
    while {1} {
        set matches {}
        set seen {}
        foreach properties [targets -target-properties] {
            if {![dict exists $properties target_id] ||
                ![dict exists $properties name]} {
                continue
            }
            lappend seen [dict get $properties name]
            if {![string match -nocase "APU*" [dict get $properties name]]} {
                continue
            }
            set digilent 0
            foreach key {jtag_cable_name jtag_cable_manufacturer jtag_cable_product} {
                if {[dict exists $properties $key] &&
                    [string match -nocase "*Digilent*" [dict get $properties $key]]} {
                    set digilent 1
                }
            }
            if {$digilent} {
                lappend matches $properties
            }
        }
        if {[llength $matches] == 1} {
            break
        }
        if {[llength $matches] > 1} {
            error "multiple Digilent-backed APU targets matched"
        }
        if {[clock milliseconds] >= $deadline} {
            error "expected one Digilent-backed APU, found none; seen: $seen"
        }
        after 250
    }
    targets [dict get [lindex $matches 0] target_id]
    puts "M11_GEM_READONLY_BEGIN"
    foreach address {
        0xF8000140 0xE000B004
        0xE000B108 0xE000B134 0xE000B138 0xE000B13C
        0xE000B140 0xE000B144 0xE000B148 0xE000B14C
        0xE000B184 0xE000B188 0xE000B18C 0xE000B190
        0xE000B194 0xE000B198 0xE000B19C 0xE000B1A0
        0xE000B1A4 0xE000B1A8 0xE000B1AC 0xE000B1B0
    } {
        puts [mrd $address]
    }
    puts "M11_GEM_READONLY_END"
} finally {
    if {$connection ne ""} {
        disconnect $connection
    }
}
exit
