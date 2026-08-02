if(CONFIG_CYCLESCOPE_CSLP_DIAGNOSTIC_CONSUMER)
    message(FATAL_ERROR
        "runtime fault tests cannot run with the CSLP diagnostic consumer")
endif()

if(CONFIG_CYCLESCOPE_CSLP_DISABLE_PUSH_TEST)
    message(FATAL_ERROR
        "runtime fault tests cannot run with the DISABLE/CONFIG/ENABLE test")
endif()

target_sources(
    ${COMPONENT_LIB}
    PRIVATE
        "${CMAKE_CURRENT_LIST_DIR}/cyclescope_receiver_runtime_fault_test.cpp"
)
target_include_directories(
    ${COMPONENT_LIB}
    PRIVATE
        "${CMAKE_CURRENT_LIST_DIR}"
)
target_compile_definitions(
    ${COMPONENT_LIB}
    PRIVATE
        CONFIG_CYCLESCOPE_RUNTIME_FAULT_TEST=1
)
