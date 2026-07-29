# CycleScope Zynq-7010 PL

本目录保留复制进来的 Vivado 2018.3 `project_1.xpr` 作为硬件管脚和旧 AD9226/SPI 行为参考。新的正式实现不直接修改旧工程，而采用以下源码事实源：

- `rtl/`：可综合 SystemVerilog；
- `sim/`：自检 testbench；
- `constraints/`：板级与时序约束；
- `scripts/`：Vivado 2025.1 批处理仿真、综合和 PS/DMA 集成工程生成；
- `docs/`：PL/PS 与 SPI 接口契约；
- `build/`：全部生成物，已被 Git 忽略。

统一工具版本为 Vivado 2025.1，目标器件为 `xc7z010clg400-1`。所有命令从 `CycleScope-FPGA` 根目录执行：

```bash
source Zynq_7010_PL/scripts/xilinx_env.sh
make -C Zynq_7010_PL version
make -C Zynq_7010_PL sim
make -C Zynq_7010_PL synth
make -C Zynq_7010_PL system
```

`xilinx_env.sh` 会校验仓库路径和 `codex/FPGA` 分支，并把 HOME、Vitis 数据、日志和缓存重定向到当前 worktree 内。禁止从其他 worktree 调用这些构建入口。
