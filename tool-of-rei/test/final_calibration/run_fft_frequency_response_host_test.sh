#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/../../.." && pwd)"
build_dir="$(mktemp -d /tmp/cyclescope-fft-frequency-response.XXXXXX)"
trap 'rm -rf "${build_dir}"' EXIT

"${CXX:-g++}" \
    -std=c++20 -O1 -g \
    -Wall -Wextra -Werror -pedantic \
    -fsanitize=address,undefined -fno-omit-frame-pointer \
    -fno-sanitize-recover=all \
    -I"${repo_root}/tool-of-rei/test/fft_host_shims" \
    -I"${repo_root}/ESP32-P4/main/app" \
    "${script_dir}/fft_frequency_response_host_test.cpp" \
    "${repo_root}/tool-of-rei/test/fft_processor_host_shim.cpp" \
    "${repo_root}/ESP32-P4/main/app/fft_processor.cpp" \
    "${repo_root}/ESP32-P4/main/app/waveform_projection.cpp" \
    -o "${build_dir}/fft-frequency-response-host-test"

ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 \
UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 \
    "${build_dir}/fft-frequency-response-host-test"
