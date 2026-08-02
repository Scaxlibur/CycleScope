# CycleScope 测试夹具索引

本目录同时保存纯主机测试、离线证据重验、生成器、真实 LAN/串口/仪器夹具和 ESP-IDF 私有故障注入材料。禁止用通配符批量执行 Python 脚本。只有标为“纯主机”或显式 self-test-only / inspect-only 的入口可以在无板卡环境运行。

夹具源码和本 README 受版本控制；__pycache__、构建缓存、串口日志、pcap 和临时输出仍保持忽略。原始数据的位置与适用范围见 ../../public/测试与证据索引.md。

## 1. 纯主机：可直接运行

以下入口只在 /tmp 编译或读取本机代码；不打开串口、不发送网络包、不操作仪器。建议在仓库根目录执行。

    bash tool-of-rei/test/run_g_parameter_sweep_host_test.sh
    bash tool-of-rei/test/run_live_stream_freshness_host_test.sh
    bash tool-of-rei/test/run_measurement_format_host_test.sh
    bash tool-of-rei/test/final_calibration/run_frequency_response_host_test.sh
    bash tool-of-rei/test/final_calibration/run_fft_frequency_response_host_test.sh

    PYTHONDONTWRITEBYTECODE=1 python3 tool-of-rei/test/cslp_g_acceptance_matrix.py
    PYTHONDONTWRITEBYTECODE=1 python3 tool-of-rei/test/cslp_g_high_harmonic_matrix.py
    PYTHONDONTWRITEBYTECODE=1 python3 tool-of-rei/test/cslp_g_v3_sender_evidence.py --self-test-only
    PYTHONDONTWRITEBYTECODE=1 python3 tool-of-rei/test/cslp_v3_long_stability_evidence.py --self-test-only
    PYTHONDONTWRITEBYTECODE=1 python3 tool-of-rei/test/cslp_v4_long_stability_evidence.py --self-test-only
    PYTHONDONTWRITEBYTECODE=1 python3 tool-of-rei/test/cslp_v5_boundary_matrix_evidence.py --self-test-only
    PYTHONDONTWRITEBYTECODE=1 python3 tool-of-rei/test/cslp_v5_long_stability_evidence.py --self-test-only
    PYTHONDONTWRITEBYTECODE=1 python3 tool-of-rei/test/cslp_spectrum_hysteresis_sender.py --self-test-only

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v -s tool-of-rei/test/final_calibration -p 'test_*.py'

五个 shell runner 使用 g++ 在 /tmp 进行 ASan/UBSan 主机测试，退出时清理临时目录。final_calibration 的 unittest 依赖 numpy 和本机 WaveBench 源码，但不访问仪器、串口或网络。

运行 final_calibration unittest 前先确认当前 Python 环境可导入 numpy；缺少该依赖时会在收集阶段报 ImportError，应先补齐测试环境，而不是把它解释为标定算法失败。

补充纯主机源码包括 exact_weak_startup_vector_host_test.cpp、spectrum_projection_self_test.cpp、waveform_projection_self_test.cpp 和 stream_identity_host_test.cpp；它们暂无独立 runner，应由维护者按同样的 sanitizer 编译参数运行。fft_host_shims/ 与 fft_processor_host_shim.cpp 是 FFT 主机测试兼容层，不是独立入口。

## 2. 离线证据重验：读取归档

以下脚本重放或解析已经存在的日志、镜像帧和归档数据，不主动联网或碰硬件；多数需要显式提供归档路径。默认 /tmp 路径只是当时运行环境，不代表数据已随仓库提交。

- cslp_g_v3_sender_evidence.py
- cslp_v3_long_stability_evidence.py
- cslp_v4_filtered_residual_evidence.py、cslp_v4_long_stability_evidence.py
- cslp_v5_boundary_matrix_evidence.py、cslp_v5_filtered_residual_evidence.py、cslp_v5_long_stability_evidence.py、cslp_v5_spectrum_hysteresis_stale_recovery_evidence.py
- 对应的 adversarial_test.py：会在 /tmp 创建伪造副本以验证拒绝逻辑，但不联网或访问硬件。
- m12_p4_acceptance.py、m12_campaign_audit.py、final_calibration/calibration_core.py、final_calibration/p4_final_acceptance.py、final_calibration/p4_final_merge_audit.py。

cslp_real_fpga_pcap_replay.py 加上测量点目录和 --inspect-only 时也属于安全的离线检查；只有显式加 --confirm-network-replay 才会发送 UDP。

## 3. 无硬件访问但会写指定输出

- m12_generate_arb.py：生成 ARB 与理论清单。
- final_calibration/generate_p4_asset.py：生成频响头文件和清单；加 --install-header 会修改 ESP32-P4 应用源码。
- m12_campaign_audit.py、final_calibration/p4_final_acceptance.py、final_calibration/p4_final_merge_audit.py：向明确指定的结果目录写 JSON 或审计输出。

运行前必须先检查输出路径；不要把新结果覆盖到已带 SHA256SUMS 的历史证据根。

## 4. 真实设备夹具：默认不得直接运行

### LAN 与 P4

- capture_lvgl_screenshot.py
- cyclescope_disable_push_test.py
- cyclescope_pre_ready_wave_guard_test.py
- cyclescope_socket_runtime_fault_test.py
- cslp_spectrum_hysteresis_sender.py（非 self-test-only 模式）
- cslp_real_fpga_pcap_replay.py（确认网络重放后）

### 串口与被动镜像

- capture_p4_serial.py 会切换 RTS/DTR 并可能硬复位 P4。
- m12_passive_uart_capture.py 不写串口数据、不主动复位，但仍会打开真实 UART。
- m12_passive_mirror_capture.py 只绑定并接收 UDP 镜像，不主动发包，但仍是实时设备夹具。

### 仪器与全链路

- m12_live_campaign.py
- m12_user_to_sine_transition.py
- m12_harm_to_sine_transition.py
- final_calibration/calibration_campaign.py
- final_calibration/p4_final_live_campaign.py

这些 live campaign 的 --list 是安全规划模式；--dry-run 不碰仪器但会写输出。其余命令可能操作 DG4202、真实 LAN 或 P4，执行前必须建立新的受控采集窗口。两个 to_sine_transition 脚本即使保证最终输出 OFF，也会实际切换 DG4202 波形状态。

## 5. 私有 ESP-IDF 故障注入与调试构建

- startup_fault_test.cmake 与 cyclescope_pipeline_startup_fault_test、cyclescope_receiver_startup_fault_test、cyclescope_display_startup_fault_test。
- runtime_fault_test.cmake 与 cyclescope_receiver_runtime_fault_test。
- display_fault_test.cmake 与 cyclescope_display_lifecycle_fault_test。
- lvgl_screenshot_debug.cmake、display_fault_trace.defaults、p4_host_emulator.defaults。

这类材料不是普通主机测试：它们会把私有夹具注入 ESP-IDF 固件。后续编译、烧录、面板验证或配套 UDP 驱动都需要真实 P4。特别是 lvgl_screenshot_debug.cmake 会启用 TCP 50002 调试服务，禁止用于正式的 FPGA .2 镜像。

## 6. 新增夹具的归档规则

1. 源码、运行说明、纯主机预期结果放在本目录并提交。
2. 原始日志、pcap、波形、BIN/ELF 和大批截图放在 ../evidence/、../screenshots/ 或 /tmp 的受控归档中，不直接提交。
3. 达到可引用结论后，在 ../../public/测试与证据索引.md 和 ../项目快照.md 中补充证据根、哈希和适用范围。
4. 不修改已有 SHA256SUMS 所覆盖的历史文件；修订必须创建新的证据根。
