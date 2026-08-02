# Local-only LVGL screenshot build fragment.  It is intentionally ignored by
# Git and must be selected explicitly, for example:
#
#   idf.py -B /tmp/cyclescope-p4-shot-build \
#       -D CYCLESCOPE_LOCAL_TEST_CMAKE="$PWD/tool-of-rei/test/lvgl_screenshot_debug.cmake" \
#       build
#
# The source is not part of normal component SRCS.  Do not turn this into a
# public Kconfig feature: a formal .2 image must not listen on TCP 50002.
if(CONFIG_CYCLESCOPE_CSLP_DIAGNOSTIC_CONSUMER)
    message(FATAL_ERROR
        "LVGL screenshot debug must not run with the CSLP diagnostic consumer")
endif()

if(CONFIG_CYCLESCOPE_CSLP_DISABLE_PUSH_TEST)
    message(FATAL_ERROR
        "LVGL screenshot debug must not run with the DISABLE/CONFIG/ENABLE test")
endif()

idf_component_get_property(CYCLESCOPE_DEBUG_LVGL_LIB lvgl__lvgl COMPONENT_LIB)

target_sources(
    ${COMPONENT_LIB}
    PRIVATE
        "${CMAKE_CURRENT_LIST_DIR}/../../ESP32-P4/main/app/lvgl_screenshot_debug.cpp"
)
target_compile_definitions(
    ${COMPONENT_LIB}
    PRIVATE
        CYCLESCOPE_LVGL_SCREENSHOT_DEBUG=1
        LV_USE_SNAPSHOT=1
)

# `lv_snapshot_take_to_draw_buf()` itself is compiled inside the LVGL library,
# so the same local-only define has to reach that target as well.
target_compile_definitions(
    ${CYCLESCOPE_DEBUG_LVGL_LIB}
    PRIVATE
        LV_USE_SNAPSHOT=1
)

message(STATUS "CycleScope local LVGL screenshot debug server: enabled")
