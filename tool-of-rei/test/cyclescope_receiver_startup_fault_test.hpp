#pragma once

#include <cstdint>

namespace cyclescope {

class CslpUdpReceiver;

namespace startup_fault_test {

enum class ReceiverFailPoint : uint8_t {
    None,
    Mutex,
    EventGroup,
    NetifInit,
    EventLoop,
    EthernetInit,
    EmptyEthernetHandles,
    NetifCreate,
    NetifGlue,
    NetifAttach,
    EthEventHandler,
    IpEventHandler,
    EthernetStart,
    ReceiverTask,
    StaticIp,
};

void arm_receiver_failpoint(ReceiverFailPoint point);
bool consume_receiver_failpoint(ReceiverFailPoint point);
bool run_receiver_startup_fault_matrix(CslpUdpReceiver &receiver);

}  // namespace startup_fault_test
}  // namespace cyclescope
