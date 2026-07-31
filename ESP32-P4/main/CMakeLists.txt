idf_component_register(
    SRCS
        "main.cpp"
        "lvgl_adapter_init.c"
        "app/instrument_app.cpp"
        "app/waveform_view.cpp"
        "app/waveform_projection.cpp"
        "app/spectrum_model.cpp"
        "app/spectrum_projection.cpp"
        "app/fft_processor.cpp"
        "app/spectrum_view.cpp"
        "app/live_data_pipeline.cpp"
        "app/cslp_protocol.cpp"
        "app/cslp_frame_diagnostic.cpp"
        "app/cslp_udp_receiver.cpp"
    INCLUDE_DIRS
        "."
        "app"
    REQUIRES
        lvgl__lvgl
        espressif__esp_lvgl_adapter
        espressif__esp32_p4_function_ev_board_noglib
        espressif__esp-dsp
        espressif__ethernet_init
        esp_eth
        esp_event
        esp_netif
        esp_timer
        lwip
)

set(CYCLESCOPE_LOCAL_TEST_CMAKE "" CACHE FILEPATH
    "Optional local-only CycleScope test build fragment")
if(NOT "${CYCLESCOPE_LOCAL_TEST_CMAKE}" STREQUAL "")
    if(NOT EXISTS "${CYCLESCOPE_LOCAL_TEST_CMAKE}")
        message(FATAL_ERROR
            "Missing local test fragment: ${CYCLESCOPE_LOCAL_TEST_CMAKE}")
    endif()
    include("${CYCLESCOPE_LOCAL_TEST_CMAKE}")
endif()

idf_component_get_property(LVGL_LIB lvgl__lvgl COMPONENT_LIB)
target_compile_options(
    ${LVGL_LIB}
    PRIVATE
        -Wno-attributes
        -DLV_LVGL_H_INCLUDE_SIMPLE
)
