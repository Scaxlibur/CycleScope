# CSLP G 题 Profile v0.1 实施与验收记录

> 类型：Historical；记录时点：2026-08-02。本页保存原 Profile 拆分前的实现、性能、阶段验收和未决事项。它不能覆盖当前 [Profile Reference](../协议与接口/CSLP-G题采样与处理-Profile-v0.1.md)或[测试与证据索引](../测试与证据索引.md)。

## 身份边界

| 对象 | 记录身份 | 能证明什么 |
| --- | --- | --- |
| FPGA | `fpga-v1.0.0@038e981` | PL、PS、LAN 与 M14 QSPI 冻结基线 |
| 最终标定 | `C5DCDE41`；上游 `25030 / 516 / -6761` | 不超过 250 mVpp 的逐频补偿身份 |
| M12 | 60 个 P4 工程联调观测点 | DG 理论值到 FPGA/LAN 帧和 P4 分析，不是镜像发布审计 |
| M8/M9 | 20/20；BIN `d819641d…d8207007` | 旧应用 BIN 的完整物理审计 |
| F0 显示版 | BIN `e30d16c4…a4bac0e` | 构建、烧录和 100 帧链路烟测，不继承 M8/M9 身份 |

最终标定以 33 个拟合点冻结模型，再执行 7 个独立 holdout 且不重新拟合。holdout 最大幅值绝对误差为 0.321 mV，最大频率误差为 234.985 Hz。三个 450 mVpp 案例已确认为压缩风险并从拟合、验证和验收排除。

## FPGA PL 实施记录

- XDC 与无转接板接线统一为 `A1=D0 … A12=D11`，只使用通道 A。
- ADC 总线和 OTR 进入 IOB；生产采样相位为 210°，输入延迟模型采用模块 `tOD=3.5..7 ns`。
- ACK 输出 c2o、板外往返和 bit skew 尚未形成完整 forwarded-clock 模型；现有 STA 不能作为完整板级眼图证明。
- 三级 Q1.17 FIR 按 `÷4 × ÷4 × ÷1` 实现，逐级舍入和饱和；694 tick 群延迟随样点标签传播并补偿到时间戳。
- 两个 8,192×S16 bank 按 `FREE/CAPTURE/READY/STREAM` 转移；65 MHz AXI-Stream 经 Clock Converter 进入 100 MHz S2MM DMA。
- 192 bit 元数据快照包含 ADC tick、首样点时间戳、frame ID 和状态字，禁止逐字段跨域拼读。
- ramp、相干 sine、三音及 OTR/overflow/frame-drop 注入均通过与正式链相同的 PL→DMA→PS→CSLP/UDP 路径。

实现后 setup/hold 为 `+1.006/+0.016 ns`；资源为 LUT `6158/17600`、寄存器 `11760/35200`、BRAM tile `20.5/60`、DSP `25/80`。正式 M10 bitstream SHA-256 为 `17776782517704772c443e2a63c00015a7d8f94edf0756b20d4e73840b0e886f`。M11 的 1～3 MHz 阻带实测最差衰减下界为 72.337599 dB，4～10 MHz 上限测试为 65.996983 dB。

## Zynq PS 与 LAN 实施记录

- Vitis 2025.1 bare-metal 应用使用 lwIP RAW API；S2MM 每次提交 16,384 byte DDR 缓冲，cache 所有权按 DMA 方向处理。
- HELLO、CONFIG、ENABLE、DISABLE、STATUS、ERROR、幂等缓存、固定 12 分片、CSLP CRC、非零 frame/config 身份与延迟 DISABLE ACK 已实现。
- `timestamp_us` 来源于 PL 锁存并补偿 FIR 群延迟后的首输出样点 ADC tick；快照或换算失败时丢弃整帧。
- BSP 固定 `CONFIG_LINKSPEED100`；RTL8211F 只广告 100BASE-TX Full，并在服务启动前检查 PHY ID、PHYSR、GEM 25 MHz 和 `NWCFG`。
- IPv4 分片和重组关闭，GEM UDP checksum offload 开启；AMD lwIP 2025.1 修补受精确版本门禁保护。

双目的地诊断曾完成电脑 `.3/.4` 预验收：功能轮实收 101 帧，稳定轮实收 1,001 帧和 12,012 个 WAVE 包；两路 pcap 各有 12,121 个 CSLP 数据报，UDP checksum 有效、IPv4 无分片、内核丢包为 0。两路应用载荷 SHA-256 均为 `6850534f92f49bf3114934d1166735c401acd907178756b4710121e0dbbb38f5`。该记录只证明 Zynq 双发和电脑被动接收，不替代真实 P4 主链。

## ESP32-P4 实施记录

- P4 固定 `.3:50001`，只接受 `.2:50000`；接收 socket 请求 `SO_RCVBUF=64 KiB`、`SO_RCVTIMEO=20 ms`，UDP 与 tcpip mailbox 均为 64。
- 三个预分配帧槽使用 `FREE/ASSEMBLING/LATEST/IN_USE`；assembling 超时 50 ms，发布前复核 session、config 与 stream epoch。
- 分析使用 8,192 点 float ANSI FFT、Hann 窗、三点初值、直接正弦投影和 H1～H50 谐波族识别。
- 正式测量使用 H1 与最强两条谐波；显示链最多保留 8 条合法谱线。TIME 页按 H1 相位选择统一周期锚点。
- 普通 H1/H3/H4 和 exact-weak H1/H2 是 fail-closed 启动门禁。ARP4 内核的弱基波错误由固定 ANSI 实现隔离。
- 网络接收位于 Core 1 优先级 6，分析位于 Core 1 优先级 4，LVGL 位于 Core 0。

历史内存预算为：单帧 16 KiB、三个帧槽约 48 KiB、FFT work 64 KiB、twiddle 32 KiB、Hann 32 KiB、正频谱约 16 KiB、接收和分析栈各 8 KiB。大缓冲和两块 RGB565 画布优先使用 PSRAM，启动失败按逆序释放。

性能记录：

- 电脑合成源 10,000 帧全部接收、分析和发布；FFT 平均/最大 `17.491/50.210 ms`，无持续内存下降、WDT、panic 或复位。
- 30 分钟 36,000 帧运行中 FFT 平均/最大 `16.740/24.807 ms`，正式区间错误计数零增长。
- 真实 FPGA `.2 → .3` 连续 600 帧中 FFT 平均/最大 `16.923/24.219 ms`，接收、重组、分析、发布与 UI bridge 可见错误均为 0。
- UI 轮询周期为 250 ms；1,000 ms 无新鲜有效帧后进入 STALE。新 session-ready 本身不能恢复 LIVE。
- POWERON 到两板 session-ready 约 4.3 s，主要由 PHY 冷启动决定，不属于专家按键后的 2 秒窗口。

## 阶段验收状态

### FPGA PL

已完成通道 A 数据活动性、offset binary 位序、三级 FIR 定点验证、连续抽取、8,192 点 TLAST/DMA、OTR/FIFO/frame-drop、自检源、时序、DRC、CDC、bus-skew 与含 bitstream XSA 门禁。

尚未完成：用已知正向斜坡或直流冻结绝对模拟极性；补齐 ACK c2o、板外往返、bit skew 的完整 forwarded-clock min/max 模型和全相位 plateau。

### Zynq PS 与网络

已完成黄金报文、CRC、S16_LE、控制幂等、12 分片、UDP checksum、20 frame/s、500 us 调度、DISABLE 排空、100M Full、10,001 帧单目的长稳、镜像开关与被动比较，以及真实 P4 600 帧主链。

尚未完成：若验收要求严格证明每包 500 us 物理上线间隔，需要硬件 RX 时间戳、TAP 或交换机测试仪；普通电脑时间戳不足以完成该证明。

### ESP32-P4

已完成非法包与故障帧门禁、三缓冲所有权、8,192 点 ANSI FFT、弱基波和高阶谐波、Vpp/真 RMS、1P/3P、动态 1～8 条频谱、断链恢复、长稳与真实 FPGA 主链。

尚未完成：TIME、FFT、1P、3P 按键到真实 panel flush 不超过 2 秒的端到端取证。软件回调耗时、UI bridge 和截图不能替代物理面板证据。

### 整机

以下项目在该记录时点仍不能标为完整整机 PASS：

- `u_a`、`u_b` 与 `u_b + u_J` 全项目的统一最终身份验收；
- 电压、频率、波形、频谱和按键时限在同一交付身份下的完整证明；
- 单路 5 V、屏幕尺寸、BNC、50 Ω 线缆和真实面板刷新留证。

## 未决事项

- AD9226 source-synchronous 接口的完整板外时序、全相位 plateau 与绝对模拟极性。
- 高于 10 MHz 的 ADC 前折叠、内部电源轨/温度及探头通道交换修正；未测内容不得写成通过。
- `close()` 失败后的 socket fd 所有权注入，以及真实 P4 参与的主链/镜像故障隔离长稳。
- F0 两位小数的真实面板字号、裁切和按键到 panel flush 的人工留证。
- 开发构建通常通过 JTAG 易失下载；`fpga-v1.0.0` 的 M14 另有 QSPI 冷启动证据，两种身份不能混写。
- 更换 FFT 内核、采样 Profile 或逐频资产时，必须建立新身份并重跑对应边界、性能和长稳门禁。

## 当前替代入口

- 固定参数：[G 题采样与处理 Profile](../协议与接口/CSLP-G题采样与处理-Profile-v0.1.md)
- 系统原理：[G 题采集与分析架构](../concepts/G题采集与分析架构.md)
- 两板联调：[FPGA 与 ESP32-P4 联调](../how-to/FPGA与ESP32-P4联调.md)
- 构建与上板：[安全构建与上板](../getting-started/安全构建与上板.md)
- 证据边界：[测试与证据索引](../测试与证据索引.md)与[验收证据索引](../验收证据索引.md)
