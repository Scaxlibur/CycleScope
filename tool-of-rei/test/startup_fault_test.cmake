if(CONFIG_CYCLESCOPE_CSLP_DIAGNOSTIC_CONSUMER)
    message(FATAL_ERROR
        "startup fault tests cannot run with the CSLP diagnostic consumer")
endif()

if(CONFIG_CYCLESCOPE_CSLP_DISABLE_PUSH_TEST)
    message(FATAL_ERROR
        "startup fault tests cannot run with the DISABLE/CONFIG/ENABLE test")
endif()

target_sources(
    ${COMPONENT_LIB}
    PRIVATE
        "${CMAKE_CURRENT_LIST_DIR}/cyclescope_display_startup_fault_test.cpp"
        "${CMAKE_CURRENT_LIST_DIR}/cyclescope_pipeline_startup_fault_test.cpp"
        "${CMAKE_CURRENT_LIST_DIR}/cyclescope_receiver_startup_fault_test.cpp"
)
set_source_files_properties(
    "${CMAKE_CURRENT_LIST_DIR}/cyclescope_display_startup_fault_test.cpp"
    PROPERTIES
        COMPILE_OPTIONS "-Werror=frame-larger-than=4096"
)
target_include_directories(
    ${COMPONENT_LIB}
    PRIVATE
        "${CMAKE_CURRENT_LIST_DIR}"
)
target_compile_definitions(
    ${COMPONENT_LIB}
    PRIVATE
        CONFIG_CYCLESCOPE_STARTUP_FAULT_TEST=1
)
