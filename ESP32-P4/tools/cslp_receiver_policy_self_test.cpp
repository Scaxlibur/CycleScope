#include <cstdlib>
#include <iostream>

#include "cslp_protocol.hpp"
#include "cslp_receiver_policy.hpp"

int main()
{
    if (!cyclescope::cslp::protocol_self_test()
        || !cyclescope::receiver_policy::self_test()) {
        std::cerr << "CSLP receiver policy self-test failed\n";
        return EXIT_FAILURE;
    }

    std::cout << "CSLP protocol and receiver policy self-tests passed\n";
    return EXIT_SUCCESS;
}
