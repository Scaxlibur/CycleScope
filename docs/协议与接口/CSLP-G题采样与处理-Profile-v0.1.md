# CSLP G题采样与处理 Profile v0.1

## 1. 文档状态

- 状态：Profile v0.1 线上参数继续冻结；FPGA 与 ESP32-P4 正式主链均已落地，真实两板握手、分析、UI 链及连续 600 帧已通过，P4 联合验收已由用户确认通过，整机物理显示时限与实物条件仍待最终验收
- FPGA 冻结锚点：签名 annotated tag `fpga-v1.0.0`，指向提交 `038e981`
- ESP32-P4 当前实现：仓库根 `main` 已包含真实 CSLP/LAN 分析显示链、1～8 条动态频谱和 F0 的 `0.01 Hz` 显示格式。F0 构建 `e30d16c4…a4bac0e` 仅有构建、烧录与 100 帧烟测证据；完整 20 例审计仍绑定旧 BIN `d819641d…d8207007`，两者不得混写。
- 最近更新：2026-08-02
- 适用项目：CycleScope 周期信号测量分析装置（G 题）
- 配套协议：[CSLP UDP 通信协议 v0.1](./CSLP-UDP-通信协议-v0.1.md)
- 目的：冻结本次比赛使用的采样链参数，并分别约束 FPGA PL、Zynq PS、ESP32-P4 和模拟前端

CSLP 主协议只描述“怎样传”。本文描述本项目“传什么、两端必须做到什么”。若两份文档冲突，线上字段与状态机以 CSLP 主协议为准，本项目参数和验收目标以本文为准。

## 2. 赛题约束映射

| 赛题要求 | 工程含义 |
|---|---|
| 被测信号所有频率分量为 10 kHz～200 kHz 或 10 kHz～500 kHz | 有效测量通带必须覆盖 10 kHz～500 kHz |
| 干扰为 200 mVpp、频率不低于 1 MHz 的单频信号 | 模拟与数字滤波组合必须从 1 MHz 起提供明确抑制，且防止高频干扰混叠回有效带 |
| 频率分辨率 500 Hz | FFT 栅格必须不大于 500 Hz |
| 峰峰值、真有效值、频谱分量幅值绝对误差不大于 5 mV | 模拟增益、ADC、滤波通带、校准和谱估计的总误差必须共同满足 5 mV，不能只验证 FFT 数学公式 |
| 基频绝对误差不大于 1 kHz | 频率估计不能简单取整到错误谱峰，且必须识别基波与谐波关系 |
| 专家要求显示后 2 秒内完成 | 系统允许提前上电、建链并持续分析；计时从系统完全启动且 LAN 会话已建立后，专家按下 TIME、FFT、1P 或 3P 功能键开始，当前或下一有效帧、分析、渲染和真实面板刷新必须在 2 秒内完成，PHY 冷启动不计入该窗口 |
| 50 Ω 信号源与 BNC 输入 | 模拟前端必须有明确的 50 Ω 端接策略和输入端标定基准 |
| 单路 5 V 供电、显示屏不小于 6 英寸 | 属于整机电源和显示约束，不由 CSLP 协议解决 |

## 3. 冻结的系统架构

```text
BNC/模拟前端
    ↓
AD9226 通道 A，12 bit，65 MSPS
    ↓
Zynq PL：采样 → 码型/极性归一化 → 低通 → 16 倍抽取
    ↓ 4.0625 MS/s，S16，8192 个连续样点
PL 双缓冲 → AXI-Stream / Clock Converter
    ↓
Zynq PS：Simple S2MM DMA → CSLP 封包、CRC、UDP、20 frame/s 推送
    ├─ 主发送：100M Ethernet → ESP32-P4 `.3:50001`
    │                                  ↓
    │                    Core 1：接收重组 → 8192 点 esp-dsp ANSI FFT → 参数估计
    │                                  ↓ 只含结果或不可变数据的队列
    │                    Core 0：LVGL 显示与交互
    └─ 诊断镜像：`.4:50002` 只收同一 CSLP 应用载荷，默认关闭
```

冻结决策：

- 只使用单颗 AD9226 的通道 A。
- 原始采样率固定为 65 MSPS。
- 不使用双 ADC 时间交织，不形成 130 MSPS 数据流。
- 低通后固定 16 倍抽取。
- FPGA PL 负责确定时序的采样、滤波与抽取；Zynq PS 负责 AXI DMA、控制状态机和 UDP。
- ESP32-P4 使用 `esp-dsp` 完成最终 FFT 与测量参数估计；当前固定调用已通过弱基波门禁的 `dsps_fft2r_fc32_ansi`，不把最终 FFT 放在 Zynq PS。
- 以太网发送的是抽取后的快照，不发送 65 MSPS 原始连续流。
- FPGA唯一正式对外数据通道是LAN。`fpga-v1.0.0`源码保留历史 SPI 诊断 RTL、镜像
  BRAM、引脚和工具以解释既有证据；它们不构成本 Profile 的产品接口，不参与比赛运行、
  默认构建验收或 LAN 诊断。
- Zynq 端固定 100BASE-TX 全双工，但保留 PHY 自协商，只广告 100M Full；交换机端不得
  关闭自协商。电脑网卡协商结果不能作为 Zynq 链路速率证据。
- 双目的地 UDP 只属于 PS 诊断能力：P4 始终是唯一控制端，电脑镜像不得形成第二个
  session；正式交付构建默认 `CSLP_MIRROR_ENABLED=0`。

## 4. 固定 Profile 参数

### 4.1 参数表

| 参数 | 固定值 | 归属/说明 |
|---|---:|---|
| ADC 型号 | AD9226 | 12 bit ADC |
| ADC 通道 | A | B 通道不参与采集、DMA 或网络 |
| 原始 ADC 采样率 | 65,000,000 sample/s | 仅 FPGA 内部使用，不写入 CSLP `sample_rate_hz` |
| 抽取倍数 | 16 | 低通之后抽取 |
| 线上样点率 | 4,062,500 sample/s | `65,000,000 / 16`，写入 CONFIG/WAVE 的 `sample_rate_hz` |
| 每帧样点数 | 8,192 | 单通道 |
| P4 FFT 长度 | 8,192 | `CONFIG_DSP_MAX_FFT_SIZE_8192=y` |
| 样点格式 | S16_LE | `sample_format = 1` |
| 通道数 | 1 | `channel_count = 1` |
| 投递周期 | 50,000 us | 20 frame/s；`frame_period_us = 50000` |
| 滤波配置 | 1 | `filter_profile = 1` |
| Zynq LAN | 100BASE-TX Full | 保留自协商，只广告 100M Full |
| Zynq CSLP 服务 | `192.168.10.2:50000` | 固定服务地址和 UDP 源端口 |
| P4 正式对端 | `192.168.10.3:50001` | 唯一控制端与正式接收端 |
| 电脑诊断镜像 | `192.168.10.4:50002` | 编译期开关，best-effort，只收不控 |
| UDP 应用载荷上限 | 1,472 byte | 标准 IPv4 MTU 1500，不允许 IP 分片 |
| WAVE_DATA 头 | 72 byte | 每个满包 700 点 |
| 每帧分片数 | 12 | 前 11 包各 700 点，末包 492 点 |

P4 应在 CONFIG_SET 中显式发送上述值，不能依赖全 0 的默认配置。Zynq 必须在 CONFIG_ACK 中回报实际值；任何一项不匹配时，P4 不得开始正式测量。

正式测量帧必须置 `FILTERED`，携带 ACK 返回的非零 `config_id`，并令 `filter_profile = 1`。未置 `FILTERED`、配置身份不符或滤波编号不符的帧只能用于诊断，不能进入测量算法。

### 4.2 派生量

```text
Fs_out = 65,000,000 / 16
       = 4,062,500 Hz

FFT_bin = Fs_out / 8192
        = 495.91064453125 Hz

window_time = 8192 / Fs_out
            = 2.016492307692 ms

Nyquist_out = Fs_out / 2
            = 2.03125 MHz
```

因此 8192 点 FFT 的原生栅格已经满足“不大于 500 Hz”的要求。不要在 UI 或报告中把它写成精确 500 Hz，也不要把 50 ms 投递周期误写成采集窗长度。

10 kHz 最低频率在一个采集窗内约有 20.16 个周期；500 kHz 最高有效频率每周期约有 8.125 个抽取后样点，均可用于波形和频谱分析。

### 4.3 `filter_profile = 1` 定义

Profile 1 的逻辑名称为 `G_BAND_500K_DECIM16`：

| 项目 | 要求 |
|---|---|
| 输入样点率 | 65 MHz |
| 输出样点率 | 4.0625 MHz |
| 抽取倍数 | 16 |
| 有效通带 | 0～500 kHz |
| 通带纹波 | 目标不大于 0.05 dB，验收上限 0.1 dB |
| 阻带起点 | 1 MHz |
| 1 MHz 至原始 Nyquist 的阻带衰减 | 不小于 50 dB |
| 直流增益 | 1.0；定点量化后的实际增益必须记录并纳入校准 |
| 相位 | 线性相位；当前三级 FIR 群延迟为 694 个 65 MHz tick，已补偿到帧时间戳语义 |
| 溢出处理 | 扩位运算、舍入和饱和；禁止二进制回绕 |

50 dB 阻带可把 200 mVpp 单频干扰的理想残余压到约 0.633 mVpp，为 5 mV 总误差留出余量。这个衰减是数字滤波链的验收目标，不是“看频谱图好像低了一点”就算过关。

数字滤波无法消除已经在 AD9226 采样前混叠进 0～500 kHz 的高频能量，因此模拟前端仍必须提供抗混叠滤波。最终应从 BNC 输入端做端到端扫频和干扰测试。

当前 `fpga-v1.0.0` 已把实现冻结为三级对称 FIR：`21 taps / ÷4`、
`31 taps / ÷4`、`79 taps / ÷1`，系数均为 Q1.17，且每级系数和精确为 `2^17`。
离线独立复算得到通带纹波 `0.019259 dB`、全混叠路径最差阻带
`-67.610974 dB`，满足本节 `0.1/50 dB` 门槛。三级等效群延迟为
`10 + 4×15 + 16×39 = 694 tick`，即约 `10.676923 µs`。

## 5. 样点码型、电压单位与校准

### 5.1 原始码归一化

AD9226 支持 straight binary（偏移二进制）和 two's complement（补码）两种输出，具体模式由封装的 MODE/DFS 引脚决定。本项目使用的 `AD9226_2CH_V1.0` 模块厂家手册第 3、11 页明确配置为 straight/offset binary，零差分输入原始码为 `0x800`。`fpga-v1.0.0` 已用 raw-IOB 和真实数据完成位序/活动性复核；绝对模拟极性仍受本节后述方向基准门限制。滤波前统一转换为零中心有符号数，步骤如下：

1. 捕获 `raw_code[11:0]`。
2. 若实测确认当前模拟前端使波形极性反相，可执行固定的板级极性校正；该行为必须由
   带方向基准的正向斜坡或直流阶跃冻结，不能只靠有限幅值正弦猜测。
3. 根据硬件输出模式转换：

   ```text
   straight binary: signed_code = int(raw_corrected) - 2048
   two's complement: signed_code = sign_extend_12(raw_corrected)
   ```

4. 转换结果范围为 `-2048..2047`，装入更宽的有符号滤波数据通路。
5. 最终发送 S16 时保留上述码值尺度，不左移 4 位；滤波器直流增益保持为 1，并采用舍入、饱和转换。

模块厂家手册说明其差分模拟前端反相，厂家电压公式以 `D ^ 0xFFF` 校正；当前 RTL 则保留独立的码型转换与算术 `INVERT_POLARITY` 参数。两种校正对 straight binary 相差 1 LSB，不能把“码型转换”“模拟极性”和“bit 位序”混为一步。`fpga-v1.0.0` 固定使用
`ADC_OFFSET_BINARY=1`、`ADC_REVERSE_BITS=0`、`INVERT_POLARITY=0`，即
`A1=D0/LSB … A12=D11/MSB`。无转接板 raw-IOB 已证明 12 位均可翻转并排除整组反向；
若后续需要声明输入绝对相位或改变算术极性，仍必须补已知正向斜坡/直流证据，不能从
有限幅值正弦波猜极性。

### 5.2 OTR 与数字溢出

- `Otr_A` 必须与对应 ADC 数据按相同流水线延迟对齐后接入采样链，并在整帧范围内锁存。
- 任一原始样点出现 OTR，该帧置 `ADC_OVERRANGE`，并增加 `adc_overrange_frames`。
- FIR、FIFO、AXI-Stream 或 DMA 发生溢出时置 `FIFO_OVERFLOW` 或放弃整帧，不能静默截断。
- `ADC_OVERRANGE` 表示模拟输入超量程；`FIFO_OVERFLOW` 表示数字数据链来不及处理。两者不得共用一个计数器。

### 5.3 输入端电压换算

CSLP 元数据使用：

```text
u_uV = sample_code × scale_uV_per_lsb + offset_uV
```

要求：

- `scale_uV_per_lsb` 和 `offset_uV` 必须折算到 BNC 输入端，而不是 ADC 芯片引脚。
- 标定必须包含 50 Ω 端接、模拟增益/衰减、ADC 名义满量程、滤波器实际直流增益和板级固定偏置。
- 未校准时 `calibration_id = 0` 且清除 `CALIBRATED`；P4 可以显示“未校准”，但不得把名义换算冒充最终 5 mV 精度结果。
- 每次校准参数改变必须分配新的非零 `calibration_id`，并与测试记录关联。
- 整数微伏/LSB 的量化误差也必须计入误差预算；若实测不能满足 5 mV，应在下一协议版本升级比例格式，不能私下把字段解释成 Q 格式。

### 5.4 当前已验证校准身份（非固定协议常量）

当前 AD8065 前端＋AD9226＋FPGA 全链路已冻结一组可追溯校准身份：

| 字段 | 当前值 | 说明 |
|---|---:|---|
| `calibration_id` | 25030 | 非零，正式帧同时置 `CALIBRATED` |
| `scale_uV_per_lsb` | 516 | 100 kHz 标量参考，折算到 DG 50 Ω 设置/BNC 输入基准 |
| `offset_uV` | -6761 | 同一标量模型的输入端零偏 |

该身份由 36 个训练 case 先冻结模型，再用 7 个独立 holdout 验证；holdout 最大绝对
误差为 `0.173011 mV`、RMS 为 `0.116103 mV`。Vitis 构建只有在校准清单、频响、
不确定度、holdout 报告及其 SHA-256 全部匹配时才允许注入非零 ID。协议中的
`516/-6761`只是上游标量；`CALIBRATED` 本身只证明该标量身份有效，不能单独证明
任意消费者已经做了逐频补偿。当前 main 的 P4 在六字段身份严格匹配时额外启用
`C5DCDE41` 逐频资产，详细位置和边界见[前端增益与逐频补偿指南](../系统补偿方案/前端增益与逐频补偿指南.md)。重新校准时允许产生新 ID，本表不是永久 Profile 常量。

## 6. FPGA PL 端要求

### 6.1 ADC 接口与时序

- 只采集通道 A；通道 B 不得进入任何交织、平均或拼接逻辑。
- ADC 65 MHz 时钟、采样输入和 OTR 必须有完整 XDC 约束。
- 必须对 ADC 输出时钟延迟给出 `set_input_delay -min/-max`，并明确数据相对哪个 ADC 时钟边沿捕获。
- 建议把首级输入寄存器放入 IOB，并以 MMCM/PLL 相位或 IDELAY 留出稳定采样裕量；最终以实现后时序和板上测试为准。
- 必须检查 CDC、复位释放和 PL→AXI 时钟域，不得用“Timing Met”掩盖未约束端点。
- 验收时 `report_timing_summary` 不得存在 ADC 数据/OTR 的 unconstrained path，也不得存在未定义输入延迟。

### 6.2 滤波与抽取

- 先执行低通，再执行 16 倍抽取；禁止直接每 16 点取 1 点。
- 本Profile允许多相、分级或其他等效结构，但都必须满足4.3节指标；当前
  `fpga-v1.0.0`的三级实现已记录在6.5节，任何拓扑/系数改动都必须重新走离线、实现和
  真实信号验收，不能仍沿用该tag的证据。
- 系数、累加器、舍入和饱和位宽必须通过定点仿真确定，并保留浮点参考模型对比。
- 抽取相位在一帧内部必须固定，8192 个输出样点必须严格连续、等间隔、无重复和无缺口。
- 滤波器在捕获窗口开始前必须进入稳定状态；不得把清零后的 FIR 瞬态放进正式测量帧。
- 需要记录 FIR 群延迟和总流水线延迟，供 `timestamp_us` 补偿和报告使用。

### 6.3 AXI-Stream / DMA 边界

- 每个正式帧向 PS 提供恰好 8192 个 S16 样点和一次原子的帧元数据快照。
- 可选择“投递点后采集一个新窗口”或“从连续环形缓冲中冻结最新窗口”，但同一实现必须固定行为并保证时间戳对应第一个传输样点。
- `TLAST`、DMA 长度与 8192 点边界必须一致；短包、超长包或元数据不同步时整帧作废。
- 至少采用双缓冲：一个由 PL/DMA 写入，一个由 PS 封包读取。任何时刻都不得覆盖 PS 正在发送的缓冲。
- 缓冲忙时允许丢弃一个投递机会并计数，不允许积压成无界队列。

### 6.4 PL 自检能力

PL 必须支持绕过 ADC 的确定性测试源，至少包括：

- 12 位范围内递增斜坡。
- 可配置频率和幅值的相干正弦。
- 两到三条已知谱线的合成信号。
- 可人工触发的 OTR、FIFO overflow 和帧丢弃标志。

测试源经过与正式数据相同的滤波、抽取、DMA 和 UDP 路径，才能真正验证整条链。

### 6.5 `fpga-v1.0.0` 当前 PL 落地状态

- 接线与位序：XDC 已与 [AD9226 通道 A 接线定义](./AD9226通道A与Zynq-7010接线定义.md)
  的无转接板接线统一；正式保持 `A1=D0 … A12=D11`，只使用通道 A。
- 采样：ADC 总线和 OTR 先进入 IOB 寄存器，生产采样相位为 210°。当前 XDC 已按模块
  `tOD=3.5..7 ns`建立输入延迟，raw-IOB 实测支持该相位；但 ACK 输出 c2o、板外往返
  线延迟和 bit skew 尚未形成完整 forwarded-clock 模型，不能把现有 STA 当作完整板级
  眼图证明。
- 滤波：三级 Q1.17 FIR 和 `÷4 × ÷4 × ÷1` 已实现；对称乘加、逐级舍入和饱和均有
  定点仿真，精确 694 tick 群延迟随样点标签传播并在时间戳中补偿。
- 帧与跨域：两个 8192×S16 正式 bank 按 `FREE/CAPTURE/READY/STREAM` 转移；65 MHz
  AXI-Stream 经 Clock Converter 进入 100 MHz Simple S2MM DMA。`TLAST`只随第8192点
  握手。`{adc_tick, frame_timestamp_tick, frame_id, status_word}`使用 192-bit
  请求/应答原子快照跨域，禁止逐字段拼读。
- 自检：ramp、相干 sine、固定三音以及 OTR/overflow/frame-drop 注入均已走同一
  `PL → DMA → PS → CSLP/UDP`链路通过；frame-drop 的唯一 100 ms 时间戳间隔和状态
  增量均已实测。
- 实现结果：完整实现 setup/hold 为 `+1.006/+0.016 ns`，DRC、CDC、bus-skew 与含
  bitstream XSA 门通过。实现后资源为 LUT `6158/17600 (34.99%)`、寄存器
  `11760/35200 (33.41%)`、BRAM tile `20.5/60 (34.17%)`、DSP `25/80 (31.25%)`。
- 正式 M10 bitstream SHA-256 为
  `17776782517704772c443e2a63c00015a7d8f94edf0756b20d4e73840b0e886f`；M11 实测
  1～3 MHz 23 个阻带点的最差衰减下界为 `72.337599 dB`，4～10 MHz正式上限测试的
  最差衰减下界为 `65.996983 dB`，均高于 50 dB 门槛。

## 7. Zynq PS 端要求

### 7.1 DMA 与缓存一致性

- AXI DMA 接收缓冲必须满足对齐要求；若启用 D-Cache，必须按所有权方向正确执行 invalidate/flush，不能靠偶尔读对来判断实现正确。
- DMA 完成中断只发布完整帧句柄和对应元数据，不在中断中做 UDP 分片、CRC 或日志刷屏。
- 发送缓冲在最后一个 UDP 数据报排入协议栈之前保持不可变。
- DMA 短传、错误中断或元数据序号不一致时放弃整帧并增加统计。

### 7.2 CSLP 与 UDP

- 完整实现主协议的 HELLO、CONFIG、ENABLE、DISABLE、STATUS、ERROR 和控制幂等缓存。
- 以源端口 50000 发送，启用 UDP checksum 和逐包 CSLP CRC32。
- 正式 CONFIG 只接受本文固定 Profile 或 0 表示默认；ACK 必须回报本文固定的实际值。
- 每 50 ms 最多投递一帧。不得因为 PL 每 2.016 ms 可形成一个窗口，就把近 496 frame/s 全部塞进百兆网口。
- 当前分片调度固定为 500 us；不得为双发缩短。普通电脑 host 时间戳会受 NAPI 合并
  影响，pcap 可严格验证 checksum 和分片，但不能冒充硬件线时间戳证明最小包间隔。
- DISABLE_PUSH_ACK、配置身份和重连行为必须满足主协议，不允许旧配置帧在 UI 中闪回。
- 不得把 65 MSPS 原始流或未滤波的抽取流伪装成 `filter_profile = 1`。

### 7.3 状态与可诊断性

串口或 STATUS 至少能观察：

- 当前 session、`device_boot_id`、`config_id`、`filter_profile` 和 `calibration_id`。
- DMA 完成/错误计数、ADC overrange 帧、FIFO overflow 帧、投递丢弃帧。
- 已发送帧/包、最近 frame_id、控制重试命中和序号冲突。
- 实际帧周期、最大封包耗时、最大 UDP 排队深度。

M12镜像的`attempted/queued/send_failures/arp_unresolved/arp_requests`只通过限速串口日志
观察，不扩展v0.1 STATUS载荷，也不污染正式`frames_sent/packets_sent/frames_dropped`。

### 7.4 `fpga-v1.0.0` 当前 PS/LAN 落地状态

- Vitis 2025.1 bare-metal 应用使用 lwIP RAW API；Simple S2MM 每次提交 16,384 byte
  DDR 缓冲，DMA 完成后按所有权执行 cache invalidate，原帧保持不可变直到最后一个
  WAVE 数据报入队。
- HELLO/CONFIG/ENABLE/DISABLE、STATUS/ERROR、控制幂等缓存、固定12分片、CSLP CRC、
  非零 frame/config 身份及延迟 DISABLE ACK 均已实现并通过主机与实板测试。
- `timestamp_us`来自 PL 锁存并补偿 FIR 群延迟的首输出样点 ADC tick；PS 在启用采集
  前建立 ADC tick 与单调时间锚点。快照或换算失败会丢弃整帧，禁止退回 DMA 完成时间。
- BSP 固定 `CONFIG_LINKSPEED100`，RTL8211F 后端只广告 100BASE-TX Full，并在 UDP
  服务启动前门禁 PHY ID、PHYSR、GEM 25 MHz 时钟和 `NWCFG`。M8 只读寄存器确认
  `SLCR_GEM0_CLK_CTRL=0x00500801`、`GEM0_NWCFG=0x011F20C3`，GEM TX/RX错误计数为0。
- IPv4 分片和重组关闭，GEM TX/RX UDP checksum offload 开启；Vitis构建会对AMD
  lwIP 2025.1的生成缺陷执行精确版本门禁，补丁不匹配时 fail closed。
- `fpga-v1.0.0@038e981` 是唯一允许引用的 FPGA 发布身份；它的比赛数据路径仅为
  `PL → DMA → PS → CSLP/UDP`。tag 中的 SPI 诊断实现只用于解释历史 M10/M11 证据，
  不属于本 Profile 的运行路径、默认验收或后续维护范围。

### 7.5 双目的地 UDP 诊断镜像

详细设计与分阶段联合验收边界见
[FPGA 双目的地 UDP 发送设计规划](./FPGA双目的地UDP发送设计规划.md)。镜像不修改
CSLP v0.1线上字节，也不是第二个会话：

1. P4 `.3:50001`是唯一合法控制源；只有主发送被 lwIP 接受后才尝试镜像。
2. `.4:50002`收到与主端逐字节相同的 CSLP 应用载荷，但使用独立 pbuf、目的 MAC/IP、
   UDP端口和校验和。
3. 镜像 ARP 未达到 stable 状态时跳过当前副本，不把 UDP pbuf挂入ARP队列；最多每秒
   发送一次独立 ARP 请求。
4. 镜像分配/发送失败只更新本地独立计数，不改变主返回值、正式 STATUS、帧所有权或
   `frames_dropped`。诊断统计每5秒最多打印一次。
5. 生产构建默认关闭镜像；诊断 ELF 必须带明确 marker，关闭 ELF 不得残留活动路径。

电脑临时同时绑定`.3/.4`的独立预验收已经完成：100帧功能轮按延迟DISABLE语义实收
101帧；1,000帧专项稳定轮实收1,001帧、12,012个WAVE包。稳定pcap两路各有12,121个
CSLP数据报，UDP checksum全部有效、IPv4分片和tcpdump内核丢包均为0，两路载荷总
SHA-256同为
`6850534f92f49bf3114934d1166735c401acd907178756b4710121e0dbbb38f5`且逐包一致；
电脑被动端`network_writes=0`。这只证明Zynq双发和电脑模拟闭环。真实P4主链另已
完成`.2 → .3`握手和连续600帧零错误分析显示；其后的P4联合验收已由用户确认通过。
具体帧数、哈希、镜像身份和适用范围统一见[测试与证据索引](../测试与证据索引.md)与
[验收证据索引](../验收证据索引.md)，本文不以摘要重复改写原始归档。

## 8. ESP32-P4 端要求

### 8.1 网络接收与重组

- P4 使用静态 IPv4 `192.168.10.3/24`，只接受配置中的唯一 FPGA 对端
  `192.168.10.2:50000`，本地 UDP 端口固定为 `50001`。生产默认不启用 DHCP、诊断
  consumer、DISABLE 测试或故障夹具。
- `cslp_udp_rx` 固定在 Core 1、优先级 6；ESP-IDF Ethernet RX task 优先级为 7，
  `cs_analyze` 优先级为 4。接收任务不得调用 FFT、LVGL 或进行每包动态内存分配。
- socket 固定请求 `SO_RCVBUF=64 KiB`、`SO_RCVTIMEO=20 ms`；lwIP UDP 与 tcpip
  receive mailbox 均为 64。设置接收超时或 bind 失败必须关闭本次 socket 并重建，
  不能带着半初始化 fd 继续握手。
- 严格按来源、公共头长度/类型/flags、session、CSLP CRC、配置身份、WAVE 元数据、
  分片边界和逐片 CRC 的顺序门禁。`ENABLE_PUSH_ACK` 前的 WAVE_DATA 不得进入重组或
  有效帧统计。
- 预分配三个 8192×S16 帧槽，状态固定为 `FREE/ASSEMBLING/LATEST/IN_USE`；完整帧以
  latest-wins 发布，尚未 acquire 的旧 latest 可以被覆盖，正在 `IN_USE` 的帧不可改写。
- assembling 超时为 50 ms；缺片、冲突重复、旧 frame、元数据冲突、
  `ADC_OVERRANGE`、`FIFO_OVERFLOW` 或旧 config 帧均不得发布到分析链。
- 分析发布结果前必须再次执行 `frame_is_current()`，把 session、config 与 stream epoch
  一并纳入当前性判断，避免断链或重配置期间发布旧结果。
- DISABLE、peer-silent、socket fatal、设备重启或新会话会使活动 stream 失效并清除
  assembling/latest；旧 `IN_USE` lease 允许安全 release，但不能再成为 current。

### 8.2 FFT 与谱估计

- 分析任务 `cs_analyze` 固定在 Core 1，与接收任务分离，只读取已 acquire 的不可变
  `IN_USE` 帧；`esp-bsp` 负责板卡、显示与触摸，FFT 来自独立的
  `espressif/esp-dsp` 组件，两者职责不得混写。
- 实际算法固定为 8192 点复数 float FFT，配置
  `CONFIG_DSP_MAX_FFT_SIZE_8192=y`。FFT work、twiddle table、Hann 窗和正频谱缓冲
  只在启动时分配并初始化一次，任一步失败均禁止启动正式 receiver/analysis 链。
- ESP32-P4 的 ARP4 内核曾在 10 kHz 弱基波、特定相位输入上产生错误正谱；当前产品
  明确调用 `dsps_fft2r_fc32_ansi`。未经 exact-weak 目标板 A/B、完整题目矩阵、时限和
  长稳回归，不得恢复 ARP4，也不得通过放宽有效性门禁掩盖问题。
- 每帧先按 `scale_uV_per_lsb/offset_uV` 转为 BNC 输入端电压、求均值并去直流，再乘
  Hann 窗。单边 4097-bin 幅值按窗函数实际和补偿，除 DC/Nyquist 外乘 2；原生
  bin 宽固定为 `495.91064453125 Hz`。
- 只在 10～500 kHz 搜索候选峰；候选门限为 `max(0.5 mVpk, 带内最大峰×0.5%)`，
  先做对数三点插值，再在候选附近以直接正弦投影细化频率和峰值幅度。1 MHz 以上
  残余不进入被测谱线。
- 基频识别按谐波族匹配，不假设最大幅值峰就是 H1；允许到 H50，并要求至少检测到
  H1 与一条合法谐波才发布有效 measurement。各谱线折算回基频后按幅值与阶次加权，
  因而强高次谐波不会直接被误报为基波。
- 正式测量口径仍是“基波 + 最强两条谐波”，最多三条：在细化频率处重新投影幅相，
  Vpp 由 4096 点相位保持重构求极差，真 RMS 按
  `sqrt(sum(A_peak² / 2))` 计算。这是当前题目谐波模型下的严格等价方法，不使用
  `Vpp/(2√2)` 正弦近似。
- 显示链另保留 H1 与最强七条合法谐波，最多 8 条，按频率排序；它不改变正式
  Vpp/RMS/F0 口径。FFT 页默认显示 3 条，`−/+` 在 1～实际检测数内调节，横轴按当前
  谱线范围增加 10% 边距、单线至少 20 kHz，并钳位到 0～500 kHz。
- TIME 页使用 H1 相位在 Hann 窗中心附近选择统一上升过零锚点，分别对 1P/3P 做
  保峰投影；强高次谐波或相位 wrap 不得导致相邻帧跳到不同周期分支。
- 启动时必须运行普通 H1/H3/H4 和 exact-weak H1/H2 两个 fail-closed 门禁；后者同时
  固定原始 S16 CRC、频率、幅值、Vpp、RMS、相位、谱线数和分析时限。

### 8.3 核亲和性、优先级与任务边界

这里的“三个任务”是三个业务任务，不代表 ESP-IDF 只运行三个 FreeRTOS 任务。lwIP `tcpip`、事件循环、定时器、Idle 等系统任务仍由 ESP-IDF 管理；本节只冻结 CycleScope 业务任务的亲和性。

| 业务任务 | 当前任务名 | 核亲和性 | 当前优先级 | 职责 |
|---|---|---:|---:|---|
| LVGL worker / UI | BSP/Adapter 创建 | Core 0 | 保持 Adapter 已验证默认值 | LVGL timer、显示刷新、触摸和所有 LVGL API |
| UDP/CSLP 接收 | `cslp_udp_rx` | Core 1 | 6 | `recvfrom`、来源/CRC/分片校验、完整帧重组和发布 |
| FFT/测量分析 | `cs_analyze` | Core 1 | 4 | 8192 点 FFT、谱峰细化、Vpp/RMS/基频和结果发布 |

优先级 6/4 是当前已验证产品值，Ethernet RX 系统任务另为 7。它们不是 CSLP 线上
常量，但修改后必须重跑故障矩阵和长稳；必须保持的业务关系是：

```text
receiver_priority > analysis_priority
```

调度行为：

1. `cslp_udp_rx` 平时阻塞在 `recvfrom`，不占用 Core 1。
2. UDP 分片到达时，`cslp_udp_rx` 立即唤醒并抢占 `cs_analyze`；当前 FPGA 以 500 us
   调度12个分片，接收任务需在约5.5 ms的发送窗口内逐包及时排空socket。
3. 当前没有数据报可读时，`cslp_udp_rx` 再次阻塞，`cs_analyze` 从被抢占处继续计算。
4. Core 0 不参与 FFT 或 CSLP 重组，保持 LVGL 刷新和触摸延迟稳定。

任务间数据流固定为：

```text
Core 1                                            Core 0
cslp_udp_rx（高优先级）
    ↓ 完整帧句柄
assembling → latest → in_use（三缓冲）
    ↓
cs_analyze（较低优先级）
    ↓ 深度 1 的 latest-result 队列
                                                  LVGL timer 获取并显示
```

边界规则：

- `cslp_udp_rx` 不得执行 FFT、谱估计、波形绘制或任何 LVGL API。
- `cs_analyze` 只能读取已经 acquire 的不可变 in_use；完成后显式 release。
- 网络到分析只传递缓冲句柄和小型元数据，不通过 FreeRTOS 队列复制整块 16 KiB 样点。
- FFT 落后时允许新的完整帧覆盖尚未 acquire 的旧 latest；不得建立多帧待分析积压。
- 分析结果到 UI 使用深度 1 的 latest-result 队列；旧结果可以覆盖，不能积压。
- UI 只消费参数、谱线和不可变波形视图数据，不能读取 assembling 或 in_use。
- 当帧带 `ADC_OVERRANGE`、`FIFO_OVERFLOW`、旧 `config_id` 或校验失败时，UI 保留最近有效值并明确显示异常，不能悄悄显示错误新值。
- 大型 FFT 输入、旋转因子和工作区必须一次性分配在静态区或堆中，不得放在任务栈上。

核亲和性验收：

- 启动日志或 `xPortGetCoreID()` 确认 LVGL=Core 0、receiver=Core 1、analyze=Core 1。
- 20 frame/s、10,000 帧测试中，FFT 运行期间网络仍持续排空，完整帧不因分析抢占而丢失。
- 记录三个任务的 CPU 占用、最大连续运行时间和 stack high-water mark。
- UI 无明显卡顿，网络无持续丢包，FFT 平均耗时小于 50 ms；任一项不满足时先测量再调整优先级，不随意迁核。

### 8.4 性能与内存基线

当前数据量：

| 项目 | 约占用 |
|---|---:|
| 单帧 S16 | 16 KiB |
| 三个网络帧槽样点区 | 48 KiB，另含元数据与逐片 CRC |
| 8192 点复数 float FFT work | 64 KiB |
| 8192 点 float twiddle table | 32 KiB |
| 8192 点 float Hann 窗 | 32 KiB |
| 4097 点 float 单边幅值 | 约 16 KiB |
| receiver / analysis 任务栈 | 各 8 KiB |

四块持久 FFT 缓冲合计约 144 KiB，优先分配到 cached PSRAM，失败时才回退内部 RAM；
分析结果帧和两块 RGB565 画布也优先使用 PSRAM。不得在每帧重复创建窗表、FFT 表、
大数组或 LVGL canvas。所有启动失败路径必须按逆序释放已取得资源、保持明确失败状态，
不得泄漏资源或在准备不完整时继续启动 receiver。

当前性能事实与目标：

- 电脑合成源 10,000 帧正式长压中，receiver/analysis/publish 均为 10,000，FFT
  平均/最大 `17.491/50.210 ms`；internal 最低约 `118811 B`、pipeline PSRAM 保持约
  `28040080 B`，无持续下降、WDT、panic 或复位。
- 另一次 30 分钟 36,000 帧链路中 FFT 平均/最大 `16.740/24.807 ms`，全部正式帧完成，
  错误计数零增长。latest-frame-wins 仍是过载策略，不能为追求逐帧 UI 显示而阻塞接收。
- 真实 FPGA `.2 → P4 .3` 连续 600 帧中 FFT 平均/最大 `16.923/24.219 ms`，接收、
  重组、分析、发布和 UI bridge 的可见错误计数均为 0。
- UI 正常轮询周期为 250 ms；只有成功应用到 UI 的 current 有效帧才能续期 LIVE。
  1000 ms 无有效新帧后，会在下一次轮询内进入 ONLINE/OFFLINE STALE；新 session-ready
  本身不能恢复 LIVE，必须等新的有效 measurement。
- POWERON 到真实两板 session-ready 约 4.3 s，主要受 PHY 冷启动影响，只记录而不计入
  专家按键后的 2 秒窗口。当前 50 ms 帧周期、约 15～25 ms FFT、250 ms UI 轮询和
  小于 19 ms 的软件回调显示出充分预算；真实 panel flush 的 2 秒端到端取证仍未完成。

### 8.5 当前实现与证据边界

- 当前 main 的 P4 产品实现包含正式 `.2` 对端、协议/接收门禁、
  三缓冲、ANSI FFT、1P/3P、0～500 kHz 频谱、1～8 条谱线动态视窗、生命周期回滚和
  LIVE/STALE 状态机。既有 `v1.0.0@61eb0dc` 早于 8 谱线功能，不能代表这一锚点。
- 主机和板上测试已覆盖黄金包、ACK-loss 幂等、ENABLE 前 WAVE、DISABLE/重配置、
  非法头/CRC/session/config/元数据/分片、三缓冲所有权、socket fatal 与自动恢复；
  故障帧不会越过正式分析门禁。
- 66 例非相干 G 题参数扫、四个题目边界、高阶 H19/H20/H47/H50、弱
  `5 mVpk H1 + H49/H50`、相位 wrap 和 500 kHz 边缘均已通过。电脑合成数据的最大
  F0/Vpp/RMS/谱线频率/幅值误差远低于 `1 kHz/5 mV` 门限，但这些数字链证据不替代
  BNC、模拟前端、ADC 与 FPGA 滤波的整机误差验收。
- 正式 P4 固件已与真实 FPGA 完成
  `HELLO → CONFIG_SET → ENABLE_PUSH → measurement → UI LIVE`，连续 600 帧零错误；
  用户也已人工确认频谱可调到 `8/8` 且动态横轴正常。
- 当前显示格式为 Vpp/RMS 两位小数 mV、F0 两位小数 Hz；频谱图例频率仍按整数 Hz 显示。
  F0 构建的详情、哈希和 100 帧烟测见 [测试与证据索引](../测试与证据索引.md) 与
  [F0 显示版构建烧录证明](../evidence/F0显示版构建烧录证明/README.md)。它不替代 20 例审计。
- 真实 P4 联合验收已由用户确认通过；详细根、帧数、哈希和适用范围见
  [验收证据索引](../验收证据索引.md)。真实 panel flush 的 2 秒取证和 `close()` 返回失败后的 fd
  所有权独立注入仍按各自证据边界记录。

## 9. 模拟前端要求

虽然本文重点是数字链路，下面几项不能甩锅给 FFT：

- BNC 输入及 50 Ω 端接方式必须明确。信号源幅值标称是否基于 50 Ω 负载，要在校准和测试报告中保持一致。
- 模拟前端在 10～500 kHz 内的增益平坦度、相位和噪声必须进入误差预算。
- 需要抗混叠低通，防止高于 32.5 MHz 的能量在 65 MSPS ADC 前折叠入有效带；还应与数字滤波共同提高 1 MHz 以上干扰抑制。
- 输入范围和增益应覆盖 50～250 mVpp 被测信号叠加 200 mVpp 干扰，并尽量不触发 OTR。
- 必须从 BNC 输入端完成至少两点增益和零偏校准；如增益档位可切换，每个档位使用独立 `calibration_id`。
- 单路 5 V 供电下的基准、ADC、前端和数字电源噪声需实测，不能只引用器件典型值。

## 10. 标准启动与配置序列

```text
P4 初始化 Display/LVGL，准备 8192 点 FFT 并通过普通/exact-weak 自检
    ↓
启动 Ethernet、设置静态 `.3/24`，创建 Core 1 receiver 并绑定 UDP 50001
    ↓
生成新 session_id，发送 HELLO(max_udp_payload=1472, caps=0x1F)
    ↓
校验 HELLO_ACK 与 device_boot_id
    ↓
CONFIG_SET(4062500, 8192, 50000, S16_LE, 1ch, filter_profile=1)
    ↓
保存 CONFIG_ACK 返回的 config_id 和实际配置
    ↓
ENABLE_PUSH
    ↓
接收 12 个分片 → 原子发布完整帧 → FFT/参数估计 → UI
```

任一步失败都应停在明确状态并重试，不允许 CONFIG 失败后仍用本地默认参数硬解波形。
只有新的 current 有效 measurement 成功应用到 UI 后才进入 LIVE；session-ready 或 STATUS
不能让旧数据显示为 LIVE。系统完全启动、会话已建立并持续分析后，专家按下功能键才
开始 2 秒计时，前述 PHY/启动过程不计入该窗口。

诊断镜像开启时，电脑应先绑定`.4:50002`并从`.4` ping FPGA `.2`预热ARP，然后仍由
P4按上述原序列建立唯一session；电脑不得发送HELLO/CONFIG/ENABLE/DISABLE。镜像关闭
时不增加任何启动前置条件。

## 11. 吞吐预算

每帧：

- 样点净荷：`8192 × 2 = 16,384 byte`。
- 12 个 WAVE_DATA 的 UDP 应用数据：17,248 byte。
- 按无 VLAN、计入以太网头/FCS/前导码/帧间隙：约 144,320 bit。

20 frame/s：

- 样点净荷：2.62144 Mbit/s。
- UDP 应用数据：2.75968 Mbit/s。
- 估算线缆占用：2.8864 Mbit/s，约为 100 Mbit/s 标称速率的 2.9%。

诊断双发时主端与镜像端各承担同一份业务量，估算总线缆占用为
`2 × 2.8864 = 5.7728 Mbit/s`，约占100M链路的5.8%。一个满包上线约123 us，主包与
镜像包连续上线约246 us；当前500 us分片调度仍保留约254 us余量。

因此当前瓶颈不是平均带宽，而是 pbuf/TX descriptor、接收socket和任务调度。500 us
是已经落地的FPGA发送节拍，不由电脑网卡协商速率决定，也不得因诊断双发而缩短。

## 12. 分阶段验收清单

这里的勾选只表示对应层级已有可追溯证据。FPGA→电脑通过不能自动把ESP32-P4或整机项
改成通过；诊断镜像通过也不能替代P4主链送达证据。

### 12.1 FPGA PL

- [x] 65 MHz ADC转换/采样时钟运行正确，通道A真实数据稳定，通道B未进入链路。
- [x] 模块厂家手册确认 straight/offset binary、`A1=D0/LSB ... A12=D11/MSB`。
- [x] 无转接板raw-IOB确认12位均可翻转、`A8→D7`恢复、ORA为`0/16384`，正式位序
  保持identity；offset-binary归一化、S16符号、舍入和饱和已由仿真/测试图样覆盖。
- [ ] 若要声明输入绝对模拟极性，仍需已知正向斜坡或直流证据；当前幅值/频谱验收不
  允许从有限幅值正弦反推极性。
- [x] 当前`tOD=3.5..7 ns`模型、IOB、CDC、复位和AXIS跨域均进入时序/CDC门禁，报告
  在当前模型下无相关unconstrained path。
- [ ] 补齐ACK输出c2o、板外往返、bit skew的forwarded-clock min/max模型和完整相位
  plateau扫描；现有STA不冒充这项完成。
- [x] 浮点参考、Q1.17系数检查和定点RTL仿真通过，三级系数和均为`2^17`。
- [x] 离线通带纹波`0.019259 dB`、最差阻带`67.610974 dB`；真实F/I阶段衰减下界
  `72.337599/65.996983 dB`，均满足0.1/50 dB门槛。
- [x] 抽取后连续样点率为4.0625 MS/s，长稳帧内无缺口、重复或抽取相位跳变。
- [x] 每帧恰好8192点；TLAST、16,384-byte DMA、首样点时间戳和192-bit元数据快照一致。
- [x] OTR、FIFO overflow和frame-drop可注入并由WAVE/STATUS正确上报。
- [x] 完整实现setup/hold、DRC、CDC、bus-skew、资源和含bitstream XSA门通过。

### 12.2 Zynq PS / 网络

- [x] 主协议黄金报文、CRC和S16_LE向量测试通过。
- [x] HELLO/CONFIG/ENABLE/DISABLE及控制幂等缓存测试通过。
- [x] CONFIG_ACK与WAVE使用相同非零`config_id`，默认未校准/非零校准身份均有门禁。
- [x] 每帧严格为12包：11×700点＋492点；pcap证明无IPv4分片且UDP checksum有效。
- [x] 20 frame/s和固定500 us调度通过长稳；普通host pcap时间戳不冒充精确线间隔。
- [ ] 若最终验收要求500 us的严格线时间证明，需硬件RX时间戳、TAP或交换机镜像测试仪。
- [x] 延迟DISABLE只排空允许的活动帧，ACK后不再发送旧配置WAVE。
- [x] Zynq PHY ID/PHYSR、GEM 25 MHz和NWCFG证明100M全双工；电脑网卡速率未被误用。
- [x] 经实际交换机完成单目的10,001帧/120,012 WAVE长稳，应用、GEM、NIC和pcap错误为0。
- [x] 镜像关闭/开启Vitis构建、主发后镜像策略、passive receiver及pcap逐字节比较通过。
- [x] 电脑`.3/.4`模拟完成101帧功能轮与1,001帧专项稳定轮，退出后临时`.3`已释放。
- [x] 真实P4主端`.3:50001`已与正式FPGA`.2:50000`完成握手、首帧、100帧及连续
  600帧零错误分析/UI闭环。
- [x] 固定8192点的P4联合验收已由用户确认通过；详细帧数、哈希和证据路径统一见
  [测试与证据索引](../测试与证据索引.md)与[验收证据索引](../验收证据索引.md)。

### 12.3 ESP32-P4

- [x] 非法长度/类型/flags、CRC、session、config_id、元数据冲突、重复/缺失/旧分片、
  ENABLE前WAVE及overrange/FIFO帧均不会发布半帧或进入正式分析。
- [x] `cslp_udp_rx`持续排空socket，FFT与LVGL不在接收任务执行；三缓冲lease、
  latest覆盖和current复核已通过所有权及故障矩阵。
- [x] 8192点`esp-dsp` ANSI FFT对10～500 kHz非相干多音、题目四边界、弱5 mVpk
  分量和H19/H20/H47/H50给出正确峰位与幅度。
- [x] Hann相干增益、单边谱系数、对数三点插值、直接正弦投影细化和500 kHz边界
  已由66例参数扫及板上门禁验证。
- [x] 强谐波高于基波、弱H1与相位wrap时仍能报告正确基频；ARP4弱基波错误已由
  ANSI内核和exact-weak启动门禁封闭。
- [x] Vpp采用相位保持分量重构，真RMS采用谐波分量平方和严格等价计算，不使用
  `Vpp/(2√2)`近似。
- [x] LVGL=Core 0、receiver/analyze=Core 1，优先级为6/4；10,000帧与30分钟
  36,000帧电脑合成链无持续内存下降、WDT、panic或复位。
- [x] 新会话、设备重启、peer-silent与socket fatal后可自动恢复；旧结果保留时明确
  标为STALE，只有新的有效measurement可恢复LIVE。
- [x] TIME/FFT、1P/3P、0～500 kHz频谱和1～8条动态视窗的软件契约通过；用户已
  人工确认`8/8`与动态横轴。
- [x] 正式P4与真实FPGA完成单目的连续600帧零错误接收、分析和UI bridge。
- [x] 固定8192点P4联合验收已由用户确认通过；详细证据见
  [测试与证据索引](../测试与证据索引.md)与[验收证据索引](../验收证据索引.md)。
- [ ] TIME/FFT/1P/3P按键到真实panel flush不超过2秒的端到端取证仍待完成；自动
  回调耗时或LVGL截图不能替代物理面板证据。

### 12.4 整机

- [ ] 10～200 kHz、100～250 mVpp 的 `u_a` 全项目测试通过。
- [ ] 10～500 kHz、50～250 mVpp 的 `u_b` 全项目测试通过。
- [ ] 叠加 200 mVpp、`fJ >= 1 MHz` 干扰后仍满足被测信号指标。
- [ ] Vpp、真 RMS、各频谱分量幅值绝对误差均不大于 5 mV。
- [ ] 基频绝对误差不大于 1 kHz，频谱栅格为 495.91064453125 Hz。
- [ ] 1 周期/3 周期波形和正频率轴频谱显示正确。
- [ ] 每个赛题项目从启动到完成不超过 2 秒。

## 13. 仍未冻结或尚未完成的实现细节

以下内容必须在实现与实测后补入项目设计记录，但不影响本 Profile 的线上参数：

- ADC source-synchronous接口的完整板外时序模型、全相位plateau和已知正向斜坡下的
  绝对模拟极性；当前210°和identity位序可用，但这三项不能靠已有正弦测试补写。
- `>10 MHz` ADC采样前折叠、AD8065反馈取样节点、内部电源轨/温度和探头通道交换修正；
  这些按当前授权未测，不阻塞已完成的10 MHz上限，但不能在报告中伪造结论。
- P4 算法已冻结为 8192 点 ANSI FFT、Hann 窗、三点初值、直接正弦投影
  细化及谐波族识别，不再属于“待选型”；若更换ARP4或其他内核，必须重新通过
  exact-weak、66例参数扫、题目矩阵、性能和长稳门禁。
- P4已冻结`SO_RCVBUF=64 KiB`、UDP/tcpip mailbox各64及三帧槽；剩余网络项是
  `close()`返回失败后的fd所有权独立注入，以及真实P4参与的主链/镜像故障隔离和
  10,000帧双发长稳。
- F0 两位小数 `Hz` 与 mV 格式已有构建/烧录/100 帧烟测记录，但实际面板字号、裁切与
  按键到真实 panel flush 的验收仍待人工留证；`v1.0.0@61eb0dc` 不得静默移动，也不能
  被写成包含 8 谱线或当前显示格式。
- 系统完全启动、LAN会话建立并持续分析后，TIME/FFT/1P/3P按键到真实panel flush的
  2秒证据仍待采集；LVGL软件回调、UI bridge或截图只证明软件阶段。
- 500 us发送节拍已经在FPGA代码中冻结；若需要严格证明每个包的物理上线间隔，仍需
  硬件时间戳或专用网络测试手段，普通电脑NAPI时间戳不足以完成该项。
- 当前bitstream和ELF使用JTAG易失下载；若整机要求脱离调试器冷启动，启动介质、镜像
  选择和回滚策略仍需单独冻结。现阶段禁止把“首个可用版本”误写成已经烧录QSPI。

## 14. 最容易混淆的四个数

| 数值 | 正确含义 | 不是什么 |
|---:|---|---|
| 65,000,000 sample/s | AD9226 原始采样率 | 不是 UDP 波形的 `sample_rate_hz` |
| 4,062,500 sample/s | 抽取后、线上样点率 | 不是帧率 |
| 2.016492 ms | 一帧 8192 点覆盖的时间窗 | 不是投递间隔 |
| 50 ms | 两帧快照的投递周期 | 不是采样周期，也不参与 FFT 频率轴换算 |

P4 构造 FFT 频率轴时只使用 `4,062,500 / 8,192`。拿 65 MHz 或 50 ms 去算频率轴，结果都会很有戏剧性，只是比赛现场大概没人笑得出来。
