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
| 唯一对外链路 | AXI-Stream → AXI DMA S2MM → PS DDR → CSLP/UDP over LAN |
| 板级诊断 | 内部测试源/故障注入 → 同一 DMA/CSLP/UDP 链路 → 电脑端验收 |

## 时钟与复位

| 名称 | 频率/域 | 用途 |
|---|---|---|
| `sys_clk_50m` | 50 MHz，板级输入 | Clocking Wizard 参考时钟 |
| `adc_clk_out` | 65 MHz，输出到 AD9226 | ADC 转换时钟 |
| `adc_sample_clk` | 65 MHz，相对转换时钟 210° | 在当前板级实测稳定区用 IOB 寄存器锁存输入；300°会采到单周期 ORA/数据瞬态，不能作为正式相位。锁存结果随后驱动滤波、帧缓存和 AXI-Stream |
| `FCLK_CLK0` | 100 MHz，PS 输出 | AXI-Lite、AXIS Clock Converter 出口、DMA S2MM/M_AXI、控制/状态 |

复位统一低有效。ADC 域复位必须在 Clocking Wizard `locked` 后同步释放；PS AXI 域由 `proc_sys_reset` 生成。跨域控制和状态至少经过双触发器同步，计数器使用握手快照而不是逐位直接采样。

## AD9226 归一化

1. `AD9226_2CH_V1.0` 厂家手册确认模块为 straight/offset binary，正式配置
   `ADC_OFFSET_BINARY=1`，执行 `signed_code = raw_code - 2048`。
2. `ADC_OFFSET_BINARY=0` 时，对 12 bit 补码符号扩展。
3. `INVERT_POLARITY` 在码型转换后执行算术极性翻转，`-2048` 翻转时饱和到 `2047`。
4. 厂家手册说明模块模拟差分前端反相，并以 `D ^ 0xFFF` 换算输入电压；最终
   `INVERT_POLARITY` 仍须用已知正向斜坡实测冻结，不能用 bit reverse 代替。
5. `Otr_A` 与原始样点同拍进入前端；16 个原始样点中任意一次 OTR 都传播到对应抽取样点，帧缓存再做整帧 sticky 锁存。
6. 原始 ADC 总线与 OTR 必须先进入 IOB 寄存器，码型转换在下一拍完成；禁止把 offset-binary 算术塞回外部输入时序路径。
7. 厂家手册第 5～6 页已经冻结 `A1=D0/LSB ... A12=D11/MSB`，正式保持
   `ADC_REVERSE_BITS=0`。若 raw-IOB 发现局部跳线交换，必须实现明确 permutation
   或修正物理跳线；禁止启用整组反向来掩盖局部错误。

## AXI-Stream 数据帧

| 信号 | 约束 |
|---|---|
| `TDATA[15:0]` | 一个 S16 样点，二进制位型不做网络字节序转换 |
| `TKEEP[1:0]` | 固定 `2'b11` |
| `TVALID/TREADY` | 标准 AXI4-Stream 握手；反压时所有输出保持稳定 |
| `TLAST` | 仅第 8192 个样点置 1，且必须与该样点同时握手 |
| 帧间交错 | 禁止；前一帧完成或丢弃后才能发布新帧 |

AXI DMA 首版固定使用 Simple S2MM，每次由 PS 预先提交 16384 byte 缓冲。正式数据先以 `adc_sample_clk` 进入 AXIS Clock Converter，再以 `FCLK_CLK0` 进入 DMA；DMA 的 S_AXIS_S2MM、M_AXI_S2MM 与 AXI-Lite 均工作在 `FCLK_CLK0`。这是因为 AXI DMA 7.1 的 S_AXIS_S2MM 与 M_AXI_S2MM 固定共用 `m_axi_s2mm_aclk`，不能仅靠 DMA 的异步模式拆分这两个接口。

## AXI-Lite 控制与状态

使用 AXI GPIO 承载固定寄存器语义。固定基地址为 DMA `0x40400000`、CONTROL
`0x41200000`、STATUS `0x41210000`、FRAME_TIMESTAMP `0x41220000` 和
ADC_CLOCK `0x41230000`。

### CONTROL（PS → PL，32 bit）

| 位 | 名称 | 语义 |
|---:|---|---|
| 0 | `capture_enable` | 1 允许 50 ms 投递；0 停止产生新正式帧 |
| 1 | `clear_stats` | 上升沿清除 PL sticky 状态和丢帧计数 |
| 2 | `test_pattern` | 1 选择内部测试源，0 使用真实 AD9226 |
| 4:3 | `test_mode` | 0 ramp；1 sine；2 固定 multitone；3 保留 |
| 5 | `inject_otr_toggle` | 每次翻转请求下一接受帧置一次 OTR |
| 6 | `inject_overflow_toggle` | 每次翻转置一次 overflow sticky |
| 7 | `inject_frame_drop_toggle` | 每次翻转跳过下一投递机会并增加 dropped 计数 |
| 19:8 | `test_amplitude` | 测试图样幅度，范围 `0..2047` |
| 31:20 | reserved | 必须写 0 |

### TEST_PHASE_INCREMENT（PS → PL，CONTROL channel 2，32 bit）

32-bit NCO phase increment，仅 sine 使用。对 8192 点、4.0625 MS/s 输出帧，
相干频点使用 `phase_increment = output_DFT_bin × 32768`；当前软件门禁 bin
`1..1008`。静态 mode/amplitude/increment 必须在 capture 启用前配置。

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

### FRAME_TIMESTAMP（PL → PS，64 bit）

最近发布帧首个输出样点对应的 65 MHz 原始 ADC tick。三级线性相位 FIR 的精确群
延迟为 `10 + 4×15 + 16×39 = 694 tick`；标签随每级数据传播，并在首输出样点写入
帧缓存时锁存补偿后的 tick。AXI 反压不得改变该值。

### ADC_CLOCK（PL → PS，64 bit）

连续 65 MHz ADC tick，用于 PS 在 capture 开启前建立启动单调时间锚点。
`{adc_tick, frame_timestamp_tick, frame_id, status_word}` 作为一个 192-bit 请求/应答
快照跨到 100 MHz 域；软件必须验证 frame_id 前后读取一致，禁止拼接不同代数据。

PS 用锚点把 `frame_timestamp_tick` 换算为 CSLP `timestamp_us`，支持四舍五入、
长时间运算和自然 `uint64_t` 回绕。快照或换算失败时丢弃该帧并报告 metadata
failure；禁止静默回退到 DMA 完成时刻减固定延迟。

## 内部 LAN 自检源

- ramp：12-bit 递增并回绕，验证完整样点通路、分片和帧连续性。
- sine：可配置 amplitude 与 NCO increment；推荐 bin 256、amplitude 1600。
- multitone：固定输出 bin 96、320、736，即 `47.607421875 kHz`、
  `158.69140625 kHz`、`364.990234375 kHz`，幅度权重约 `1/2、1/4、1/4`。
- 故障注入只用于诊断 ELF；PS 在首帧完整上传后触发：OTR 标记下一接受帧一次；overflow 设置 sticky；
  frame-drop 真实跳过下一投递机会并递增 dropped。注入 drop 后相邻接收帧首样点
  时间差应由约 50 ms 变为约 100 ms。
- 正式 ELF 必须使用真实 ADC、fault mask 0。所有图样和故障只经同一 LAN 数据链
  验收，不建立第二套对外通道。

## 遗留 SPI 实现（非契约）

范围变更前实现的 SPI RTL、镜像 BRAM、引脚和离线工具暂时保留，以避免在 M10 同时
引入无关结构性重构；它们不启用、不接线、不测试、不验收、不维护，也不得作为 LAN
故障诊断路径。历史协议与接线记录只用于解释现有源码，不构成当前产品接口承诺。

## 缓冲所有权

| 状态 | ADC/滤波写 | AXI-Stream 读 |
|---|---|---|
| FREE | 可取得 | 否 |
| CAPTURE | 唯一写者 | 否 |
| READY/STREAM | 否 | 唯一正式读者 |

两个正式 bank 只允许在上述状态间转移。AXI-Stream 未完成时，新捕获可以写另一个 bank，但若发布点仍无可释放 bank，则丢弃新帧并递增 `frames_dropped`，不得覆盖正式读者。
