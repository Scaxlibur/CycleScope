#ifndef CSLP_PLATFORM_CONFIG_H
#define CSLP_PLATFORM_CONFIG_H

#include "xparameters.h"

#if defined(XPAR_XEMACPS_0_BASEADDR)
#define PLATFORM_EMAC_BASEADDR XPAR_XEMACPS_0_BASEADDR
#elif defined(XPAR_XEMACPS_BASEADDR)
#define PLATFORM_EMAC_BASEADDR XPAR_XEMACPS_BASEADDR
#else
#error "No Zynq PS GEM base address in xparameters.h"
#endif
#ifndef PLATFORM_ZYNQ
#define PLATFORM_ZYNQ
#endif

#endif
