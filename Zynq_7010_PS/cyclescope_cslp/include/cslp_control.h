#ifndef CSLP_CONTROL_H
#define CSLP_CONTROL_H

#include "cslp_protocol.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif
#define CSLP_CONTROL_CACHE_ENTRIES 16U
#define CSLP_CONTROL_MAX_REQUEST_PAYLOAD 20U
#define CSLP_CONTROL_MAX_RESPONSE_BYTES 64U
#define CSLP_CONTROL_CACHE_RETENTION_US 2000000ULL

typedef struct {
    uint32_t sample_rate_hz;
    uint32_t frame_sample_count;
    uint32_t frame_period_us;
    uint8_t sample_format;
    uint8_t channel_count;
    uint16_t filter_profile;
} cslp_active_config_t;

typedef struct {
    bool occupied;
    uint32_t session_id;
    uint32_t message_seq;
    uint8_t message_type;
    uint8_t payload[CSLP_CONTROL_MAX_REQUEST_PAYLOAD];
    uint16_t payload_bytes;
    uint8_t response[CSLP_CONTROL_MAX_RESPONSE_BYTES];
    uint16_t response_bytes;
    uint64_t expires_at_us;
} cslp_control_cache_entry_t;

typedef struct {
    uint32_t cache_hits;
    uint32_t sequence_conflicts;
    uint32_t requests_executed;
    uint32_t requests_dropped;
    uint32_t cache_busy;
} cslp_control_stats_t;

typedef struct {
    uint32_t device_boot_id;
    uint32_t session_id;
    uint16_t data_port;
    uint32_t receiver_caps;
    cslp_device_state_t state;
    cslp_status_code_t last_error;
    bool config_valid;
    bool capture_requested;
    bool disable_pending;
    uint32_t next_config_id;
    uint32_t active_config_id;
    cslp_active_config_t config;
    cslp_control_cache_entry_t cache[CSLP_CONTROL_CACHE_ENTRIES];
    int pending_disable_cache_index;
    cslp_control_stats_t stats;
} cslp_control_t;

typedef enum {
    CSLP_CONTROL_DROP = 0,
    CSLP_CONTROL_RESPONSE,
    CSLP_CONTROL_DEFERRED
} cslp_control_result_t;

void cslp_control_init(cslp_control_t *control, uint32_t device_boot_id);

cslp_control_result_t cslp_control_handle(
    cslp_control_t *control,
    const uint8_t *request,
    size_t request_bytes,
    uint16_t source_port,
    uint64_t now_us,
    bool wave_tx_busy,
    uint8_t *response,
    size_t response_capacity,
    size_t *response_bytes);

cslp_control_result_t cslp_control_poll(
    cslp_control_t *control,
    uint64_t now_us,
    bool wave_tx_busy,
    uint8_t *response,
    size_t response_capacity,
    size_t *response_bytes);

bool cslp_control_has_session(const cslp_control_t *control);
bool cslp_control_push_enabled(const cslp_control_t *control);

#ifdef __cplusplus
}
#endif

#endif
