#ifndef CSLP_PLATFORM_H
#define CSLP_PLATFORM_H

#include "lwip/arch.h"

void init_platform(void);
void cleanup_platform(void);
void platform_setup_timer(void);
void platform_enable_interrupts(void);
u64_t get_time_ms(void);

#ifdef SDT
void init_timer(void);
void TimerCounterHandler(void *callback, u32_t status_event);
#endif

#endif
