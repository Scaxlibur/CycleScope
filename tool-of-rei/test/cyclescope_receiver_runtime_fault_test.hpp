#pragma once

#include <cstdint>

namespace cyclescope {

class CslpUdpReceiver;

namespace runtime_fault_test {

enum class ReceiverRuntimeFailPoint : uint8_t {
    None,
    SocketCreate,
    ReceiveTimeout,
    Bind,
    RecvfromFatalActive,
};

void arm_receiver_runtime_failpoint(ReceiverRuntimeFailPoint point);
bool consume_receiver_runtime_failpoint(ReceiverRuntimeFailPoint point);
void note_receiver_socket_opened(int socket_fd);
void note_receiver_socket_closed(int socket_fd, int close_result);
bool run_receiver_runtime_fault_matrix(CslpUdpReceiver &receiver);

}  // namespace runtime_fault_test
}  // namespace cyclescope
