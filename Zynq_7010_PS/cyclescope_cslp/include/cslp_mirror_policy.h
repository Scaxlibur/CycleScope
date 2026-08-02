#ifndef CSLP_MIRROR_POLICY_H
#define CSLP_MIRROR_POLICY_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum {
    CSLP_FANOUT_PRIMARY = 0,
    CSLP_FANOUT_MIRROR = 1
} cslp_fanout_destination_t;

typedef struct {
    bool enabled;
    uint32_t datagrams_attempted;
    uint32_t datagrams_queued;
    uint32_t send_failures;
    uint32_t arp_unresolved;
} cslp_mirror_stats_t;

typedef bool (*cslp_fanout_send_fn)(void *context,
                                    cslp_fanout_destination_t destination,
                                    const uint8_t *bytes,
                                    size_t length);

/*
 * The primary result is the only business result.  A mirror miss or failure is
 * deliberately reduced to local diagnostics so it cannot disturb acquisition,
 * frame ownership, or the CSLP session state machine.
 */
static inline bool cslp_send_primary_then_mirror(
    cslp_mirror_stats_t *mirror,
    bool mirror_arp_ready,
    const uint8_t *bytes,
    size_t length,
    cslp_fanout_send_fn send,
    void *context)
{
    if (send == NULL ||
        !send(context, CSLP_FANOUT_PRIMARY, bytes, length))
        return false;
    if (mirror == NULL || !mirror->enabled)
        return true;
    if (!mirror_arp_ready) {
        ++mirror->arp_unresolved;
        return true;
    }

    ++mirror->datagrams_attempted;
    if (send(context, CSLP_FANOUT_MIRROR, bytes, length))
        ++mirror->datagrams_queued;
    else
        ++mirror->send_failures;
    return true;
}

#endif
