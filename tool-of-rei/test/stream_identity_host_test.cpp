#include <cstdlib>
#include <iostream>

#include "cslp_receiver_policy.hpp"

int main()
{
    using cyclescope::receiver_policy::stream_identity_is_current;

    const bool pass = cyclescope::receiver_policy::self_test()
                      && stream_identity_is_current(0x11223344U, 0xAABBCCDDU,
                                                    9U, 0x11223344U,
                                                    0xAABBCCDDU, 9U)
                      && !stream_identity_is_current(0x11223344U, 0xAABBCCDEU,
                                                     10U, 0x11223344U,
                                                     0xAABBCCDDU, 9U)
                      && !stream_identity_is_current(0x11223344U, 0xAABBCCDDU,
                                                     10U, 0x11223344U,
                                                     0xAABBCCDDU, 9U);
    if (!pass) {
        std::cerr << "stream identity host self-test FAIL\n";
        return EXIT_FAILURE;
    }
    std::cout << "stream identity host self-test PASS\n";
    return EXIT_SUCCESS;
}
