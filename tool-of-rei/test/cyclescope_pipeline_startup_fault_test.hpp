#pragma once

#include <cstdint>

namespace cyclescope::startup_fault_test {

enum class PipelineFailPoint : uint8_t {
    None,
    FftWorkBuffer,
    FftTableBuffer,
    HannWindowBuffer,
    PositiveSpectrumBuffer,
    FftTableInitialization,
    SelfTestSampleBuffer,
    StartupSelfTestGate,
    AnalysisFrame,
    UiQueue,
    AnalysisTask,
};

void arm_pipeline_failpoint(PipelineFailPoint point);
bool consume_pipeline_failpoint(PipelineFailPoint point);
bool run_pipeline_startup_fault_matrix();

}  // namespace cyclescope::startup_fault_test
