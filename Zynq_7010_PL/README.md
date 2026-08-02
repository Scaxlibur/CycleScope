# CycleScope Zynq-7010 PL

旧 Vivado/PS 参考工程已经移除；当前实现只采用以下源码事实源：

- `rtl/`：可综合 SystemVerilog；
- `sim/`：自检 testbench；
- `constraints/`：板级与时序约束；
- `scripts/`：Vivado 2025.1 批处理仿真、综合和 PS/DMA 集成工程生成；
- `docs/`：PL/PS LAN 接口契约与历史诊断记录；
- `build/`：全部生成物，已被 Git 忽略。

统一工具版本为 Vivado 2025.1，目标器件为 `xc7z010clg400-1`。所有命令从仓库根目录的 `main` 分支执行：

正式方案为 LAN-only：唯一对外数据链路是
`AXI-Stream → AXI DMA → PS → CSLP/UDP`。仓库中已有的 SPI RTL、镜像 BRAM、
引脚、离线工具和历史接线文档暂时保留，但不启用、不使用、不测试、不维护，也不
作为构建或板级验收的前置条件。

因此默认 `make sim` 不再运行遗留 SPI testbench，`make synth-core` 也不再检查
SPI 专用 CDC 属性；如果以后 LAN 改动使遗留 SPI 失效，可以直接删除该实现，
不得为修复它阻塞 LAN 里程碑。

```bash
source Zynq_7010_PL/scripts/xilinx_env.sh
make -C Zynq_7010_PL version
make -C Zynq_7010_PL sim
make -C Zynq_7010_PL synth
make -C Zynq_7010_PL system
make -C Zynq_7010_PL bitstream
```

`system` 保持为综合和无 bitstream XSA 门禁；`bitstream` 额外执行布局布线、
post-route 时序/CDC/DRC/bus-skew 检查，并生成：

- `build/system/hardware/cyclescope_system.bit`
- `build/system/hardware/cyclescope_system_with_bitstream.xsa`

只有 `SYSTEM_IMPLEMENTATION_PASS` 与 `BITSTREAM_XSA_CONTENT_PASS` 同时出现，
才允许进入 JTAG 下载。

`AD9226_2CH_V1.0` 厂家手册已经确认 `A1=D0/LSB ... A12=D11/MSB`，且模块
输出为 straight/offset binary；正式源码保持 `ADC_REVERSE_BITS=0`。手册还说明
模拟差分前端反相，极性校正必须与位序分开验证。曾生成但未下载的整组反向镜像仅为
历史诊断证据，不得再把它当作接线修复。

实际跳线局部交换和采样相干性使用独立 raw-IOB ILA 构建检查，不覆盖正式
`build/system/`：

```bash
make -C Zynq_7010_PL raw-iob-analysis-test
make -C Zynq_7010_PL raw-iob-ila-0
make -C Zynq_7010_PL raw-iob-ila-30
make -C Zynq_7010_PL raw-iob-ila-90
make -C Zynq_7010_PL raw-iob-ila-150
make -C Zynq_7010_PL raw-iob-ila-210
make -C Zynq_7010_PL raw-iob-ila-240
make -C Zynq_7010_PL raw-iob-ila-300
make -C Zynq_7010_PL raw-iob-ila-345
make -C Zynq_7010_PL raw-iob-ila-348
make -C Zynq_7010_PL raw-iob-ila-351
make -C Zynq_7010_PL raw-iob-ila-354
```

正式采样相位为 210°：新版接线的 raw-IOB 实测中，该相位的数据随受控输入变化且
ORA 为 `0/16384`；300°则捕获到仅持续一个 65 MHz 周期的 ORA/数据瞬态，因此只
保留为诊断候选，不再作为正式相位。诊断入口允许 0°～330°的 30°粗扫，以及
345°/348°/351°/354°的边沿细扫；每个候选仍须独立通过既有 1 ns 输入时序门禁，
允许构建不等于允许下载。0°保留用于离线边界证明，若门禁失败不得上板；357°按
现有模型会跌破门禁，因此不提供构建入口。诊断产物位于对应的
`build/diagnostic/raw-iob-ila-p<phase>/hardware/`，必须同时通过
probe、IOB、ADC 输入时序、全局时序、DRC/CDC 和 bit/ltx 哈希门禁。捕获脚本默认
只做 dry-run，且不会编程 FPGA、复位 PS、下载 ELF、写 QSPI 或访问 MIO47；实板
运行前先用 `scripts/capture_adc_raw_ila.tcl --help` 审计完整边界。

`xilinx_env.sh` 会校验仓库路径和 `codex/FPGA` 分支，并把 HOME、Vitis 数据、日志和缓存重定向到当前 worktree 内。禁止从其他 worktree 调用这些构建入口。

PS 侧 RTL8211F 固定按 `100 Mb/s` 全双工链路使用：Vivado 将 GEM0 默认参考
时钟配置为 IO PLL `/8/5`，即 25 MHz。PHY 依赖板上冷上电复位；当前实板禁止
用 MIO47 脉冲复位，因为该路径会使 RTL8211F 在全部 MDIO 地址上失联。该约束
与 Vitis BSP 的 `CONFIG_LINKSPEED100` 及 RTL8211F 只广告 `100BASE-TX Full`
必须同时保持一致。

电脑和 Zynq 分别与交换机建立链路，电脑网卡的协商速率不能证明 Zynq 端速率。
Zynq 端只以生成的 GEM0 25 MHz 时钟、GEM `NWCFG` 和 RTL8211F `PHYSR`
为验收证据。
