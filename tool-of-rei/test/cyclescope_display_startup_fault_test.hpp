#pragma once

#include <cstdint>

#include "lvgl.h"
#include "lvgl_adapter_init.h"

namespace cyclescope::startup_fault_test {

struct DisplayStartupFaultTestAccess;

enum class DisplayFailPoint : uint8_t {
    None,
    WaveformCanvasBuffer,
    SpectrumCanvasBuffer,
};

enum class DisplayLifecycleEvent : uint8_t {
    WaveformFailpointConsumed,
    SpectrumFailpointConsumed,
    WaveformCreated,
    SpectrumCreated,
    SpectrumDestroyed,
    WaveformDestroyed,
    UiRootDestroyed,
};

void arm_display_failpoint(DisplayFailPoint point);
bool consume_display_failpoint(DisplayFailPoint point);
void note_display_lifecycle_event(DisplayLifecycleEvent event);
bool run_display_canvas_startup_fault_matrix(lv_display_t *display);
bool run_display_stack_startup_fault_matrix(const bsp_display_cfg_t *cfg);

}  // namespace cyclescope::startup_fault_test
