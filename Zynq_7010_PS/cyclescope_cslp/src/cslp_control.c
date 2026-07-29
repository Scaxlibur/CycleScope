#include "cslp_control.h"

#include <string.h>

static bool is_control_request(uint8_t message_type)
{
    return message_type == CSLP_MSG_HELLO ||
           message_type == CSLP_MSG_CONFIG_SET ||
           message_type == CSLP_MSG_ENABLE_PUSH ||
           message_type == CSLP_MSG_DISABLE_PUSH;
}

static uint8_t ack_type_for(uint8_t request_type)
{
    switch (request_type) {
    case CSLP_MSG_HELLO:
        return CSLP_MSG_HELLO_ACK;
    case CSLP_MSG_CONFIG_SET:
        return CSLP_MSG_CONFIG_ACK;
    case CSLP_MSG_ENABLE_PUSH:
        return CSLP_MSG_ENABLE_PUSH_ACK;
    case CSLP_MSG_DISABLE_PUSH:
        return CSLP_MSG_DISABLE_PUSH_ACK;
    default:
        return 0U;
    }
}

static uint16_t expected_payload_bytes(uint8_t request_type)
{
    switch (request_type) {
    case CSLP_MSG_HELLO:
        return 8U;
    case CSLP_MSG_CONFIG_SET:
        return 20U;
    case CSLP_MSG_ENABLE_PUSH:
    case CSLP_MSG_DISABLE_PUSH:
        return 0U;
    default:
        return UINT16_MAX;
    }
}

static void clear_cache(cslp_control_t *control)
{
    memset(control->cache, 0, sizeof(control->cache));
    control->pending_disable_cache_index = -1;
}

static void expire_cache(cslp_control_t *control, uint64_t now_us)
{
    unsigned int index;

    for (index = 0; index < CSLP_CONTROL_CACHE_ENTRIES; ++index) {
        cslp_control_cache_entry_t *entry = &control->cache[index];
        if (control->pending_disable_cache_index == (int)index)
            continue;
        if (entry->occupied && now_us >= entry->expires_at_us) {
            memset(entry, 0, sizeof(*entry));
        }
    }
}

static int find_cache_key(cslp_control_t *control,
                          const cslp_header_t *header)
{
    unsigned int index;

    for (index = 0; index < CSLP_CONTROL_CACHE_ENTRIES; ++index) {
        const cslp_control_cache_entry_t *entry = &control->cache[index];
        if (entry->occupied && entry->session_id == header->session_id &&
            entry->message_type == header->message_type &&
            entry->message_seq == header->message_seq)
            return (int)index;
    }
    return -1;
}

static int reserve_cache(cslp_control_t *control,
                         const cslp_header_t *header,
                         const uint8_t *payload,
                         uint64_t now_us)
{
    unsigned int index;

    expire_cache(control, now_us);
    for (index = 0; index < CSLP_CONTROL_CACHE_ENTRIES; ++index) {
        cslp_control_cache_entry_t *entry = &control->cache[index];
        if (!entry->occupied) {
            memset(entry, 0, sizeof(*entry));
            entry->occupied = true;
            entry->session_id = header->session_id;
            entry->message_type = header->message_type;
            entry->message_seq = header->message_seq;
            entry->payload_bytes = header->payload_bytes;
            if (header->payload_bytes != 0U)
                memcpy(entry->payload, payload, header->payload_bytes);
            entry->expires_at_us = now_us + CSLP_CONTROL_CACHE_RETENTION_US;
            return (int)index;
        }
    }
    ++control->stats.cache_busy;
    return -1;
}

static bool payload_matches(const cslp_control_cache_entry_t *entry,
                            const uint8_t *payload,
                            uint16_t payload_bytes)
{
    return entry->payload_bytes == payload_bytes &&
           (payload_bytes == 0U ||
            memcmp(entry->payload, payload, payload_bytes) == 0);
}

static size_t build_ack(const cslp_control_t *control,
                        uint8_t request_type,
                        cslp_status_code_t status,
                        uint32_t session_id,
                        uint32_t message_seq,
                        uint64_t now_us,
                        uint8_t *response,
                        size_t response_capacity)
{
    uint8_t payload[28] = {0};
    uint16_t payload_bytes;
    uint8_t response_type = ack_type_for(request_type);

    if (response_type == 0U)
        return 0U;
    cslp_write_u16be(payload, (uint16_t)status);

    switch (request_type) {
    case CSLP_MSG_HELLO:
        payload_bytes = 16U;
        if (status == CSLP_STATUS_OK) {
            payload[2] = CSLP_VERSION;
            cslp_write_u32be(payload + 4, CSLP_REQUIRED_CAPS);
            cslp_write_u32be(payload + 8, CSLP_PROFILE_FRAME_SAMPLES);
            cslp_write_u32be(payload + 12, control->device_boot_id);
        }
        break;
    case CSLP_MSG_CONFIG_SET:
        payload_bytes = 28U;
        if (status == CSLP_STATUS_OK) {
            cslp_write_u32be(payload + 4, control->active_config_id);
            cslp_write_u32be(payload + 8, control->config.sample_rate_hz);
            cslp_write_u32be(payload + 12,
                             control->config.frame_sample_count);
            cslp_write_u32be(payload + 16, control->config.frame_period_us);
            payload[20] = control->config.sample_format;
            payload[21] = control->config.channel_count;
            cslp_write_u16be(payload + 22, control->config.filter_profile);
            cslp_write_u32be(payload + 24, CSLP_PROFILE_FRAME_SAMPLES);
        }
        break;
    case CSLP_MSG_ENABLE_PUSH:
    case CSLP_MSG_DISABLE_PUSH:
        payload_bytes = 4U;
        break;
    default:
        return 0U;
    }

    return cslp_build_message(response, response_capacity, response_type,
                              session_id, message_seq, now_us, 0U, payload,
                              payload_bytes);
}

static size_t build_error(uint32_t session_id,
                          uint32_t message_seq,
                          uint64_t now_us,
                          uint8_t offending_type,
                          cslp_status_code_t status,
                          uint8_t *response,
                          size_t response_capacity)
{
    uint8_t payload[12] = {0};

    cslp_write_u16be(payload, (uint16_t)status);
    payload[2] = offending_type;
    cslp_write_u32be(payload + 4, message_seq);
    return cslp_build_message(response, response_capacity, CSLP_MSG_ERROR,
                              session_id, message_seq, now_us, 0U, payload,
                              (uint16_t)sizeof(payload));
}

static bool store_response(cslp_control_cache_entry_t *entry,
                           const uint8_t *response,
                           size_t response_bytes)
{
    if (entry == NULL || response == NULL || response_bytes == 0U ||
        response_bytes > sizeof(entry->response))
        return false;
    memcpy(entry->response, response, response_bytes);
    entry->response_bytes = (uint16_t)response_bytes;
    return true;
}

static bool config_value_valid(uint32_t requested, uint32_t fixed)
{
    return requested == 0U || requested == fixed;
}

static cslp_status_code_t validate_config_payload(const uint8_t *payload)
{
    if (!config_value_valid(cslp_read_u32be(payload + 0),
                            CSLP_PROFILE_SAMPLE_RATE_HZ) ||
        !config_value_valid(cslp_read_u32be(payload + 4),
                            CSLP_PROFILE_FRAME_SAMPLES) ||
        !config_value_valid(cslp_read_u32be(payload + 8),
                            CSLP_PROFILE_FRAME_PERIOD_US) ||
        !config_value_valid(payload[12], CSLP_PROFILE_SAMPLE_FORMAT) ||
        !config_value_valid(payload[13], CSLP_PROFILE_CHANNEL_COUNT) ||
        !config_value_valid(cslp_read_u16be(payload + 14),
                            CSLP_PROFILE_FILTER) ||
        cslp_read_u32be(payload + 16) != 0U)
        return CSLP_STATUS_BAD_CONFIG;
    return CSLP_STATUS_OK;
}

static void apply_fixed_config(cslp_control_t *control)
{
    control->config.sample_rate_hz = CSLP_PROFILE_SAMPLE_RATE_HZ;
    control->config.frame_sample_count = CSLP_PROFILE_FRAME_SAMPLES;
    control->config.frame_period_us = CSLP_PROFILE_FRAME_PERIOD_US;
    control->config.sample_format = CSLP_PROFILE_SAMPLE_FORMAT;
    control->config.channel_count = CSLP_PROFILE_CHANNEL_COUNT;
    control->config.filter_profile = CSLP_PROFILE_FILTER;
    control->active_config_id = control->next_config_id++;
    if (control->active_config_id == 0U) {
        control->active_config_id = 1U;
        control->next_config_id = 2U;
    }
    if (control->next_config_id == 0U)
        control->next_config_id = 1U;
    control->config_valid = true;
    control->state = CSLP_DEVICE_READY;
}

void cslp_control_init(cslp_control_t *control, uint32_t device_boot_id)
{
    if (control == NULL)
        return;
    memset(control, 0, sizeof(*control));
    control->device_boot_id = device_boot_id == 0U ? 1U : device_boot_id;
    control->state = CSLP_DEVICE_IDLE;
    control->next_config_id = 1U;
    control->pending_disable_cache_index = -1;
}

static cslp_control_result_t return_uncached_status(
    const cslp_control_t *control,
    const cslp_header_t *header,
    cslp_status_code_t status,
    uint64_t now_us,
    uint8_t *response,
    size_t response_capacity,
    size_t *response_bytes)
{
    (void)control;
    *response_bytes = build_ack(control, header->message_type, status,
                                header->session_id, header->message_seq,
                                now_us, response, response_capacity);
    return *response_bytes == 0U ? CSLP_CONTROL_DROP : CSLP_CONTROL_RESPONSE;
}

cslp_control_result_t cslp_control_handle(
    cslp_control_t *control,
    const uint8_t *request,
    size_t request_bytes,
    uint16_t source_port,
    uint64_t now_us,
    bool wave_tx_busy,
    uint8_t *response,
    size_t response_capacity,
    size_t *response_bytes)
{
    cslp_header_t header;
    cslp_parse_result_t parse_result;
    const uint8_t *payload;
    cslp_status_code_t status = CSLP_STATUS_OK;
    int cache_index;
    size_t built_bytes;
    bool new_session_accepted = false;

    if (response_bytes != NULL)
        *response_bytes = 0U;
    if (control == NULL || request == NULL || response == NULL ||
        response_bytes == NULL)
        return CSLP_CONTROL_DROP;

    parse_result = cslp_parse_datagram(request, request_bytes, &header);
    if (parse_result != CSLP_PARSE_OK) {
        ++control->stats.requests_dropped;
        return CSLP_CONTROL_DROP;
    }
    if (header.flags != 0U) {
        ++control->stats.requests_dropped;
        return CSLP_CONTROL_DROP;
    }
    if (!is_control_request(header.message_type)) {
        if (header.session_id == 0U || header.session_id != control->session_id) {
            ++control->stats.requests_dropped;
            return CSLP_CONTROL_DROP;
        }
        *response_bytes = build_error(header.session_id, header.message_seq,
                                      now_us, header.message_type,
                                      CSLP_STATUS_UNSUPPORTED, response,
                                      response_capacity);
        return *response_bytes == 0U ? CSLP_CONTROL_DROP
                                     : CSLP_CONTROL_RESPONSE;
    }
    if (header.payload_bytes > CSLP_CONTROL_MAX_REQUEST_PAYLOAD) {
        ++control->stats.requests_dropped;
        return CSLP_CONTROL_DROP;
    }
    if (header.message_type != CSLP_MSG_HELLO &&
        (header.session_id == 0U || header.session_id != control->session_id)) {
        ++control->stats.requests_dropped;
        return CSLP_CONTROL_DROP;
    }

    payload = request + header.header_bytes;
    expire_cache(control, now_us);
    cache_index = find_cache_key(control, &header);
    if (cache_index >= 0) {
        cslp_control_cache_entry_t *entry = &control->cache[cache_index];
        if (!payload_matches(entry, payload, header.payload_bytes)) {
            ++control->stats.sequence_conflicts;
            return return_uncached_status(control, &header,
                                          CSLP_STATUS_SEQ_CONFLICT, now_us,
                                          response, response_capacity,
                                          response_bytes);
        }
        if (entry->response_bytes == 0U)
            return CSLP_CONTROL_DEFERRED;
        if (response_capacity < entry->response_bytes)
            return CSLP_CONTROL_DROP;
        memcpy(response, entry->response, entry->response_bytes);
        *response_bytes = entry->response_bytes;
        ++control->stats.cache_hits;
        return CSLP_CONTROL_RESPONSE;
    }

    if (header.version != CSLP_VERSION)
        status = CSLP_STATUS_BAD_VERSION;
    else if (header.header_bytes != CSLP_COMMON_HEADER_BYTES ||
             header.payload_bytes != expected_payload_bytes(header.message_type))
        status = CSLP_STATUS_BAD_LENGTH;

    if (header.message_type == CSLP_MSG_HELLO && status == CSLP_STATUS_OK) {
        uint16_t data_port = cslp_read_u16be(payload);
        uint16_t max_payload = cslp_read_u16be(payload + 2);
        uint32_t caps = cslp_read_u32be(payload + 4);

        if (header.session_id == 0U || data_port == 0U || data_port != source_port)
            status = CSLP_STATUS_BAD_CONFIG;
        else if (max_payload != CSLP_MAX_UDP_PAYLOAD ||
                 caps != CSLP_REQUIRED_CAPS)
            status = CSLP_STATUS_UNSUPPORTED;

        if (status == CSLP_STATUS_OK) {
            clear_cache(control);
            control->session_id = header.session_id;
            control->data_port = data_port;
            control->receiver_caps = caps;
            control->state = CSLP_DEVICE_IDLE;
            control->last_error = CSLP_STATUS_OK;
            control->config_valid = false;
            control->capture_requested = false;
            control->disable_pending = false;
            control->active_config_id = 0U;
            memset(&control->config, 0, sizeof(control->config));
            new_session_accepted = true;
        }
    } else if (header.message_type == CSLP_MSG_CONFIG_SET &&
               status == CSLP_STATUS_OK) {
        if (control->disable_pending ||
            control->state == CSLP_DEVICE_PUSH_ENABLED)
            status = CSLP_STATUS_BAD_STATE;
        else
            status = validate_config_payload(payload);
    } else if (header.message_type == CSLP_MSG_ENABLE_PUSH &&
               status == CSLP_STATUS_OK) {
        if (control->disable_pending || !control->config_valid ||
            (control->state != CSLP_DEVICE_READY &&
             control->state != CSLP_DEVICE_PUSH_ENABLED))
            status = CSLP_STATUS_BAD_STATE;
    } else if (header.message_type == CSLP_MSG_DISABLE_PUSH &&
               status == CSLP_STATUS_OK) {
        if (control->disable_pending)
            status = CSLP_STATUS_BUSY;
        else if (control->state != CSLP_DEVICE_PUSH_ENABLED &&
                 control->state != CSLP_DEVICE_READY)
            status = CSLP_STATUS_BAD_STATE;
    }

    if (!new_session_accepted) {
        cache_index = reserve_cache(control, &header, payload, now_us);
        if (cache_index < 0)
            return return_uncached_status(control, &header, CSLP_STATUS_BUSY,
                                          now_us, response, response_capacity,
                                          response_bytes);
    } else {
        cache_index = reserve_cache(control, &header, payload, now_us);
        if (cache_index < 0)
            return CSLP_CONTROL_DROP;
    }

    if (status == CSLP_STATUS_OK) {
        switch (header.message_type) {
        case CSLP_MSG_CONFIG_SET:
            apply_fixed_config(control);
            break;
        case CSLP_MSG_ENABLE_PUSH:
            control->state = CSLP_DEVICE_PUSH_ENABLED;
            control->capture_requested = true;
            break;
        case CSLP_MSG_DISABLE_PUSH:
            control->capture_requested = false;
            if (control->state == CSLP_DEVICE_PUSH_ENABLED && wave_tx_busy) {
                control->disable_pending = true;
                control->pending_disable_cache_index = cache_index;
                ++control->stats.requests_executed;
                return CSLP_CONTROL_DEFERRED;
            }
            control->state = CSLP_DEVICE_READY;
            break;
        default:
            break;
        }
    } else {
        control->last_error = status;
    }

    built_bytes = build_ack(control, header.message_type, status,
                            header.session_id, header.message_seq, now_us,
                            response, response_capacity);
    if (!store_response(&control->cache[cache_index], response, built_bytes)) {
        memset(&control->cache[cache_index], 0,
               sizeof(control->cache[cache_index]));
        return CSLP_CONTROL_DROP;
    }
    *response_bytes = built_bytes;
    ++control->stats.requests_executed;
    return CSLP_CONTROL_RESPONSE;
}

cslp_control_result_t cslp_control_poll(
    cslp_control_t *control,
    uint64_t now_us,
    bool wave_tx_busy,
    uint8_t *response,
    size_t response_capacity,
    size_t *response_bytes)
{
    cslp_control_cache_entry_t *entry;
    size_t built_bytes;
    int index;

    if (response_bytes != NULL)
        *response_bytes = 0U;
    if (control == NULL || response == NULL || response_bytes == NULL ||
        !control->disable_pending || wave_tx_busy)
        return CSLP_CONTROL_DROP;

    index = control->pending_disable_cache_index;
    if (index < 0 || index >= (int)CSLP_CONTROL_CACHE_ENTRIES)
        return CSLP_CONTROL_DROP;
    entry = &control->cache[(unsigned int)index];
    if (!entry->occupied || entry->message_type != CSLP_MSG_DISABLE_PUSH)
        return CSLP_CONTROL_DROP;

    control->state = CSLP_DEVICE_READY;
    control->disable_pending = false;
    control->pending_disable_cache_index = -1;
    built_bytes = build_ack(control, CSLP_MSG_DISABLE_PUSH, CSLP_STATUS_OK,
                            entry->session_id, entry->message_seq, now_us,
                            response, response_capacity);
    if (!store_response(entry, response, built_bytes))
        return CSLP_CONTROL_DROP;
    entry->expires_at_us = now_us + CSLP_CONTROL_CACHE_RETENTION_US;
    *response_bytes = built_bytes;
    return CSLP_CONTROL_RESPONSE;
}

bool cslp_control_has_session(const cslp_control_t *control)
{
    return control != NULL && control->session_id != 0U;
}

bool cslp_control_push_enabled(const cslp_control_t *control)
{
    return control != NULL && control->capture_requested &&
           control->state == CSLP_DEVICE_PUSH_ENABLED &&
           !control->disable_pending;
}
