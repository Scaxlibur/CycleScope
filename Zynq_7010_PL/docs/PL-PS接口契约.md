# CycleScope PL/PS 接口契约

## 冻结参数

| 项目 | v0.1 固定值 |
|---|---:|
| FPGA | XC7Z010-1CLG400 |
| 正式 ADC | AD9226 通道 A，12 bit |
| 原始采样率 | 65,000,000 sample/s |
| 抽取 | 低通后 16 倍 |
| 输出样点率 | 4,062,500 sample/s |
| 输出格式 | 单通道 S16，保留 `-2048..2047` 尺度 |
| 帧长 | 8192 点，16384 byte |
| 投递周期 | 3,250,000 个 65 MHz 周期，即 50 ms |
| 正式链路 | AXI-Stream → AXI DMA S2MM → PS DDR → CSLP/UDP |
| 诊断链路 | 冻结 BRAM → SPI，只读且无反压权 |

## 时钟与复位

| 名称 | 频率/域 | 用途 |
|---|---|---|
| `sys_clk_50m` | 50 MHz，板级输入 | Clocking Wizard 参考时钟 |
| `adc_clk_out` | 65 MHz，输出到 AD9226 | ADC 转换时钟 |
| `adc_sample_clk` | 65 MHz，相位偏移 | ADC 输入、滤波、帧缓存和 AXI-Stream |
| `FCLK_CLK0` | 100 MHz，PS 输出 | AXI-Lite、DMA M_AXI、控制/状态 |
| `spi_sclk` | 不高于 10 MHz，外部输入 | SPI 诊断读端口 |

复位统一低有效。ADC 域复位必须在 Clocking Wizard `locked` 后同步释放；PS AXI 域由 `proc_sys_reset` 生成。跨域控制和状态至少经过双触发器同步，计数器使用握手快照而不是逐位直接采样。

## AD9226 归一化

1. `ADC_OFFSET_BINARY=1` 时，`signed_code = raw_code - 2048`。
2. `ADC_OFFSET_BINARY=0` 时，对 12 bit 补码符号扩展。
3. `INVERT_POLARITY` 在码型转换后执行算术极性翻转，`-2048` 翻转时饱和到 `2047`。
4. 正式默认不翻转；MODE/DFS 与板级极性必须在上板阶段实测后才能改默认值。
5. `Otr_A` 与原始样点同拍进入前端；16 个原始样点中任意一次 OTR 都传播到对应抽取样点，帧缓存再做整帧 sticky 锁存。

## AXI-Stream 数据帧

| 信号 | 约束 |
|---|---|
| `TDATA[15:0]` | 一个 S16 样点，二进制位型不做网络字节序转换 |
| `TKEEP[1:0]` | 固定 `2'b11` |
| `TVALID/TREADY` | 标准 AXI4-Stream 握手；反压时所有输出保持稳定 |
| `TLAST` | 仅第 8192 个样点置 1，且必须与该样点同时握手 |
| 帧间交错 | 禁止；前一帧完成或丢弃后才能发布新帧 |

AXI DMA 首版固定使用 Simple S2MM，每次由 PS 预先提交 16384 byte 缓冲。S_AXIS_S2MM 工作在 `adc_sample_clk`，M_AXI_S2MM 与 AXI-Lite 工作在 `FCLK_CLK0`，DMA 必须启用异步时钟支持。

## AXI-Lite 控制与状态

首版使用 AXI GPIO 承载固定寄存器语义：

### CONTROL（PS → PL，32 bit）

| 位 | 名称 | 语义 |
|---:|---|---|
| 0 | `capture_enable` | 1 允许 50 ms 投递；0 停止产生新正式帧 |
| 1 | `clear_stats` | 上升沿清除 PL sticky 状态和丢帧计数 |
| 2 | `test_pattern` | 1 选择内部递增测试样点，0 使用 AD9226 |
| 31:3 | reserved | 必须写 0 |

### FRAME_ID（PL → PS，32 bit）

最近一次成功发布给 AXI-Stream 的非零 `frame_id`。

### STATUS（PL → PS，32 bit）

| 位 | 名称 | 语义 |
|---:|---|---|
| 0 | `frame_pending` | 有一帧正在等待/通过 AXI-Stream 发送 |
| 1 | `capture_active` | 正在收集 8192 点窗口 |
| 2 | `last_frame_otr` | 最近发布帧采集期间出现 OTR |
| 3 | `overflow_sticky` | 因上一帧未释放而丢弃投递帧 |
| 15:4 | `frames_dropped[11:0]` | PL 侧丢帧计数低 12 bit，饱和 |
| 31:16 | reserved | 读为 0 |

控制状态只用于运行控制和诊断；CSLP `timestamp_us`、完整统计和配置事务由 PS 维护。

## SPI 诊断接口

- SPI mode 0，`CS_N` 低有效，MSB-first，`SCLK ≤ 10 MHz`。
- `0xA0 GET_INFO`：返回 magic、版本、状态、冻结帧代号和样点数。
- `0xA1 READ_SAMPLES`：命令后发送 16 bit 大端样点偏移和 1 byte dummy，随后按 S16_LE 连续读样点。
- 每次 `CS_N` 拉低时锁存诊断 bank 与 generation。读取前后 generation 不一致时，主机必须丢弃整次事务并重试。
- SPI 不得拉低 AXI `TREADY`、阻止 DMA 释放或推迟 50 ms 投递。异常长事务允许返回失效数据，但不得影响正式链路。

## 缓冲所有权

| 状态 | ADC/滤波写 | AXI-Stream 读 | SPI 读 |
|---|---|---|---|
| FREE | 可取得 | 否 | 可读旧代，可能失效 |
| CAPTURE | 唯一写者 | 否 | 不保证，代号变化后必须丢弃 |
| READY/STREAM | 否 | 唯一正式读者 | 可并行只读 |

两个正式 bank 只允许在上述状态间转移。AXI-Stream 未完成时，新捕获可以写另一个 bank，但若发布点仍无可释放 bank，则丢弃新帧并递增 `frames_dropped`，不得覆盖正式读者。
