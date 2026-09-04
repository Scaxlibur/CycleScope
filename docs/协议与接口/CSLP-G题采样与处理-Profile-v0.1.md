# CSLP G 题采样与处理 Profile v0.1

> 类型：Reference；状态：Current。Profile v0.1 的线上参数保持冻结。线上字段与状态机以 [CSLP UDP 通信协议](CSLP-UDP-通信协议-v0.1.md)为准；实现状态和验收结果以[测试与证据索引](../测试与证据索引.md)为准。

本页只定义 CycleScope G 题链路「传什么」以及各层必须遵守的稳定边界。系统设计原因见 [G 题采集与分析架构](../concepts/G题采集与分析架构.md)，两板启动和诊断步骤见 [FPGA 与 ESP32-P4 联调](../how-to/FPGA与ESP32-P4联调.md)，拆分前的实施与验收记录见[历史记录](../history/CSLP-G题-Profile-v0.1-实施与验收记录.md)。

## 适用范围与职责

- 适用项目：CycleScope 周期信号测量分析装置 G 题链路。
- FPGA PL：采集 AD9226 通道 A，完成码型归一化、低通和 16 倍抽取。
- Zynq PS：通过 DMA 取得完整帧，执行 CSLP 控制状态机、分片、CRC 和 UDP 发送。
- ESP32-P4：重组完整帧，完成 FFT、频响补偿、测量、1P/3P 投影和显示。
- 电脑镜像：仅接收与主端相同的 CSLP 应用载荷，默认关闭，不形成第二个会话。

## 固定参数

| 参数 | 固定值 | 说明 |
| --- | ---: | --- |
| ADC 型号 | AD9226 | 12 bit ADC |
| ADC 通道 | A | 通道 B 不参与采集、DMA 或网络 |
| 原始采样率 | 65,000,000 sample/s | 仅在 FPGA 内部使用 |
| 抽取倍数 | 16 | 必须先低通，再抽取 |
| 线上样点率 | 4,062,500 sample/s | 写入 CONFIG/WAVE 的 `sample_rate_hz` |
| 每帧样点数 | 8,192 | 单通道连续样点 |
| P4 FFT 长度 | 8,192 | `CONFIG_DSP_MAX_FFT_SIZE_8192=y` |
| 样点格式 | S16_LE | `sample_format = 1` |
| 通道数 | 1 | `channel_count = 1` |
| 投递周期 | 50,000 us | 20 frame/s；`frame_period_us = 50000` |
| 滤波配置 | 1 | `filter_profile = 1` |
| Zynq LAN | 100BASE-TX Full | 保留自协商，只广告 100M Full |
| Zynq CSLP 服务 | `192.168.10.2:50000` | 固定服务地址和 UDP 源端口 |
| P4 正式端点 | `192.168.10.3:50001` | 唯一控制端与正式接收端 |
| 电脑诊断镜像 | `192.168.10.4:50002` | best-effort，只收不控 |
| UDP 应用载荷上限 | 1,472 byte | 标准 IPv4 MTU 1500，不允许 IP 分片 |
| WAVE_DATA 头 | 72 byte | 每个满包容纳 700 点 |
| 每帧分片数 | 12 | 前 11 包各 700 点，末包 492 点 |

P4 必须在 CONFIG_SET 中显式发送这些值。Zynq 必须在 CONFIG_ACK 中返回实际配置；任一项不匹配时，不得开始正式测量。

正式测量帧必须置 `FILTERED`，使用 ACK 返回的非零 `config_id`，并满足 `filter_profile = 1`。未置 `FILTERED`、配置身份不符或滤波编号不符的帧只能用于诊断。

## 派生量

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

原生 FFT 栅格满足「不大于 500 Hz」的要求。UI 和报告不得将其写成精确 500 Hz，也不得把 50 ms 投递周期写成采集窗长度。

10 kHz 最低频率在单个采集窗内约有 20.16 个周期；500 kHz 最高有效频率每周期约有 8.125 个抽取后样点。

## `filter_profile = 1`

Profile 1 的逻辑名称为 `G_BAND_500K_DECIM16`：

| 项目 | 要求 |
| --- | --- |
| 输入样点率 | 65 MHz |
| 输出样点率 | 4.0625 MHz |
| 抽取倍数 | 16 |
| 有效通带 | 0～500 kHz |
| 通带纹波 | 目标不大于 0.05 dB，验收上限 0.1 dB |
| 阻带起点 | 1 MHz |
| 1 MHz 至原始 Nyquist 的阻带衰减 | 不小于 50 dB |
| 直流增益 | 1.0；定点量化后的实际增益进入校准 |
| 相位 | 线性相位；群延迟补偿到帧时间戳语义 |
| 溢出处理 | 扩位、舍入和饱和；禁止二进制回绕 |

冻结实现采用三级对称 Q1.17 FIR：`21 taps / ÷4`、`31 taps / ÷4`、`79 taps / ÷1`，每级系数和均为 `2^17`。等效群延迟为 `694` 个 65 MHz tick，即约 `10.676923 µs`。

50 dB 阻带可将 200 mVpp 单频干扰的理想残余降至约 0.633 mVpp。该指标只约束数字滤波；数字滤波无法消除已经在 AD9226 采样前混叠进测量带的能量，模拟前端仍须承担抗混叠职责。

## 样点码型与位序

`AD9226_2CH_V1.0` 模块配置为 straight/offset binary，零差分输入原始码为 `0x800`。固定实现使用：

```text
ADC_OFFSET_BINARY = 1
ADC_REVERSE_BITS  = 0
INVERT_POLARITY   = 0
A1=D0/LSB … A12=D11/MSB
```

归一化步骤：

1. 捕获 `raw_code[11:0]`。
2. 只有在已知正向斜坡或直流阶跃证据支持时，才允许执行板级极性校正。
3. 按硬件输出模式转换：

   ```text
   straight binary: signed_code = int(raw_corrected) - 2048
   two's complement: signed_code = sign_extend_12(raw_corrected)
   ```

4. 转换结果范围为 `-2048..2047`，进入更宽的有符号滤波通路。
5. 发送 S16 时保持该码值尺度，不左移 4 位；滤波链使用舍入和饱和。

码型转换、模拟极性和 bit 位序是三个独立问题。有限幅值正弦只能支持幅值与频谱验收，不能单独冻结绝对模拟极性。

## OTR 与数字溢出

- `Otr_A` 与 ADC 数据按相同流水线延迟对齐，并在整帧范围锁存。
- 任一原始样点出现 OTR 时，帧置 `ADC_OVERRANGE`，并增加 `adc_overrange_frames`。
- FIR、FIFO、AXI-Stream 或 DMA 溢出时置 `FIFO_OVERFLOW` 或放弃整帧，不能静默截断。
- `ADC_OVERRANGE` 与 `FIFO_OVERFLOW` 使用不同计数器，分别表示模拟超量程和数字数据链拥塞。

## 电压与校准语义

CSLP 元数据使用：

```text
u_uV = sample_code × scale_uV_per_lsb + offset_uV
```

- `scale_uV_per_lsb` 和 `offset_uV` 折算到 BNC 输入端，不是 ADC 芯片引脚。
- 标定必须包含 50 Ω 端接、模拟增益/衰减、ADC 满量程、滤波器实际直流增益和板级偏置。
- 未校准时使用 `calibration_id = 0` 并清除 `CALIBRATED`；名义换算不能作为 5 mV 精度结论。
- 校准参数改变时分配新的非零 `calibration_id`，并绑定测试记录。
- `CALIBRATED` 只证明帧中的标量身份有效，不证明消费者已经应用逐频补偿。
- 当前运行身份和逐频资产不属于 Profile 永久常量，统一从[测试与证据索引](../测试与证据索引.md)进入。

## 模拟输入与整机边界

- `u_a` 的正式验收范围为 10～200 kHz、100～250 mVpp。
- `u_b` 的正式验收范围为 10～500 kHz、50～250 mVpp。
- 200 mVpp、频率不低于 1 MHz 的 `u_J` 是独立干扰条件，不能据此宣称任意相位叠加形成的 450 mVpp 包络属于正式连续量程。
- 干扰验收是在 `u_b` 上叠加 200 mVpp、频率不低于 1 MHz 的 `u_J`，并继续满足被测信号指标；它不改变 `u_b` 的普通输入范围。
- 450 mVpp 历史压力点已确认模拟前级压缩，已从当前标定拟合、holdout 和验收排除。
- BNC 输入、50 Ω 端接和 DG 50 Ω 设置值构成当前量值参考。
- 模拟前端在 10～500 kHz 内的增益、相位与噪声进入误差预算，并须在 ADC 前限制带外能量。
- 单路 5 V 供电、屏幕尺寸、BNC 实物和按键到真实面板刷新的时限属于整机验收，不由 CSLP 协议证明。
- 2 秒计时以系统完全启动且 LAN 会话已经建立为前提，从按下 TIME、FFT、1P 或 3P 功能键开始；PHY 冷启动不计入该窗口，当前或下一有效帧的分析、渲染与真实面板刷新必须在窗口内完成。

## 容易混淆的数值

| 数值 | 正确含义 | 不是什么 |
| ---: | --- | --- |
| 65,000,000 sample/s | AD9226 原始采样率 | 不是 UDP 波形的 `sample_rate_hz` |
| 4,062,500 sample/s | 抽取后线上样点率 | 不是帧率 |
| 2.016492 ms | 8,192 点帧覆盖的时间窗 | 不是投递间隔 |
| 50 ms | 两帧快照的投递周期 | 不是采样周期，不参与 FFT 频率轴换算 |

P4 构造频率轴时只使用 `4,062,500 / 8,192`。

## 相关文档

- [CSLP UDP 通信协议](CSLP-UDP-通信协议-v0.1.md)
- [G 题采集与分析架构](../concepts/G题采集与分析架构.md)
- [FPGA 与 ESP32-P4 联调](../how-to/FPGA与ESP32-P4联调.md)
- [安全构建与上板](../getting-started/安全构建与上板.md)
- [测试与证据索引](../测试与证据索引.md)
- [Profile v0.1 实施与验收历史](../history/CSLP-G题-Profile-v0.1-实施与验收记录.md)
