#!/usr/bin/env bash

_cyclescope_fail() {
    echo "CycleScope FPGA environment error: $*" >&2
    return 1 2>/dev/null || exit 1
}

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CYCLESCOPE_FPGA_ROOT="$(cd "${_script_dir}/../.." && pwd)"

_git_root="$(git -C "${CYCLESCOPE_FPGA_ROOT}" rev-parse --show-toplevel 2>/dev/null)" || \
    _cyclescope_fail "${CYCLESCOPE_FPGA_ROOT} is not a Git worktree"
[[ "${_git_root}" == "${CYCLESCOPE_FPGA_ROOT}" ]] || \
    _cyclescope_fail "unexpected worktree root: ${_git_root}"

_branch="$(git -C "${CYCLESCOPE_FPGA_ROOT}" branch --show-current)"
[[ "${_branch}" == "codex/FPGA" ]] || \
    _cyclescope_fail "refusing to run on branch ${_branch}; expected codex/FPGA"

export XILINX_RELEASE_ROOT="/tools/Xilinx/2025.1"
export XILINX_VIVADO="${XILINX_RELEASE_ROOT}/Vivado"
export XILINX_VITIS="${XILINX_RELEASE_ROOT}/Vitis"
[[ -x "${XILINX_VIVADO}/bin/vivado" ]] || _cyclescope_fail "Vivado 2025.1 not found"
[[ -x "${XILINX_VITIS}/bin/vitis" ]] || _cyclescope_fail "Vitis 2025.1 not found"

# Xilinx tools may otherwise write IDE/cache data below the user's real HOME.
export HOME="${CYCLESCOPE_FPGA_ROOT}/.build-home"
export XDG_CACHE_HOME="${HOME}/.cache"
export XILINX_VITIS_DATA_DIR="${HOME}/.vitis"
export VITIS_WORKSPACE="${CYCLESCOPE_FPGA_ROOT}/Zynq_7010_PS/build/workspace"
mkdir -p "${HOME}" "${XDG_CACHE_HOME}" "${XILINX_VITIS_DATA_DIR}" "${VITIS_WORKSPACE}"

export PATH="${XILINX_VIVADO}/bin:${XILINX_VITIS}/bin:${PATH}"
unset _script_dir _git_root _branch
