#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/../.." && pwd)"
build_dir="$(mktemp -d /tmp/cyclescope-measurement-format.XXXXXX)"
trap 'rm -rf "${build_dir}"' EXIT

cxx="${CXX:-g++}"
binary="${build_dir}/measurement-format-host-test"

"${cxx}" \
    -std=c++20 -O1 -g \
    -Wall -Wextra -Werror -pedantic \
    -fsanitize=address,undefined -fno-omit-frame-pointer \
    -fno-sanitize-recover=all \
    -I"${repo_root}/ESP32-P4/main/app" \
    "${script_dir}/measurement_format_host_test.cpp" \
    -o "${binary}"

ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 \
UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 \
    "${binary}"
