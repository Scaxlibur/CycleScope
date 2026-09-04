# FPGA 与 ESP32-P4 联调

> 类型：How-to；状态：Current。本文只给出两板主链的最短联调顺序。构建、下载和烧录前先完成[安全构建与上板](../getting-started/安全构建与上板.md)的门禁。

联调目标是建立唯一的 P4 控制会话，并让一个合法的 8,192 点 Profile v0.1 帧依次完成 FPGA 发送、P4 重组、测量和 UI 发布。

> [!CAUTION]
> JTAG 下载、P4 烧录、串口打开和网络重放会改变真实设备状态。没有对应授权时，只检查配置、命令和日志，不执行真实操作。

## 前置条件

- PL 仿真、综合、system XSA 和 bitstream 门禁已经通过。
- PS 主机测试和 Vitis 正式构建已经通过。
- P4 使用新的 build 与 `sdkconfig` 路径，正式 peer 为 `.2`，本地故障夹具和诊断 consumer 关闭。
- FPGA 和 P4 已接入隔离网络，信号源输出保持 OFF。
- [G 题 Profile](../协议与接口/CSLP-G题采样与处理-Profile-v0.1.md)与 [CSLP 主协议](../协议与接口/CSLP-UDP-通信协议-v0.1.md)的参数已经核对。

## 1. 核对网络角色

| 角色 | 地址 | 权限 |
| --- | --- | --- |
| Zynq/FPGA | `192.168.10.2:50000` | CSLP 服务端与 WAVE 发送端 |
| ESP32-P4 | `192.168.10.3:50001` | 唯一 HELLO/CONFIG/ENABLE/DISABLE 控制端 |
| 电脑镜像 | `192.168.10.4:50002` | 可选被动接收，默认关闭 |

电脑不得占用 UDP 50000/50001，也不得以第二个控制端发送 HELLO。历史模拟器使用的 `.5` 不属于正式两板链。

## 2. 启动 FPGA

按[安全构建与上板](../getting-started/安全构建与上板.md)先完成 JTAG dry-run，取得授权后再执行 `--execute`。下载后检查：

- bitstream、PS ELF 和当前分支与刚记录的构建一致；
- PHY ID、PHYSR、GEM 25 MHz 和 `NWCFG` 通过启动门禁；
- Zynq 只广告 100BASE-TX Full，交换机端保持自协商；
- UDP 服务监听 `.2:50000`，没有启用历史 SPI 路径。

若此前执行过 MIO47 PHY 复位实验，必须彻底断电并等待电源放净后再上电；系统复位不能恢复已经在 MDIO 上失联的 RTL8211F。

## 3. 启动 P4

烧录正式镜像前再次检查生成配置：

- peer 为 `192.168.10.2`；
- `CYCLESCOPE_LOCAL_TEST_CMAKE` 为空；
- diagnostic consumer、DISABLE lifecycle test、heap trace 和 fault fixture 关闭；
- FFT 使用 `dsps_fft2r_fc32_ansi`；
- Profile 身份为 4,062,500 sample/s、8,192 点、50,000 us、S16_LE、单通道、`filter_profile = 1`。

授权烧录并打开有界串口采集后，P4 应先完成 UI 与普通/exact-weak FFT 自检，再完成 Ethernet Link Up 和 receiver 启动。打开 CP2102N 可能切换 DTR/RTS 并复位 P4，日志时间线应将该动作与异常重启区分。

## 4. 观察控制序列

正常顺序为：

```text
P4 初始化 UI 与 FFT
  → Ethernet `.3/24` 与 UDP 50001 就绪
  → HELLO(max_udp_payload=1472, caps=0x1F)
  → HELLO_ACK + device_boot_id
  → CONFIG_SET(4062500, 8192, 50000, S16_LE, 1ch, filter_profile=1)
  → CONFIG_ACK + 非零 config_id
  → ENABLE_PUSH
  → 12 分片完整帧
  → FFT / 参数估计
  → UI LIVE
```

CONFIG 失败时必须停在明确状态并重试，不能退回本地默认值硬解波形。session-ready 或 STATUS 也不能让旧数据显示为 LIVE；只有新的 current 有效 measurement 可以恢复 LIVE。

## 5. 核对帧与分析门禁

对首个正式帧确认：

- `FILTERED`、非零 `config_id`、`filter_profile = 1` 和校准元数据符合当前证据身份；
- 12 个分片为前 11 包各 700 点、末包 492 点，无 IPv4 分片；
- 来源、公共头、session、CSLP CRC、WAVE 元数据和逐片 CRC 全部通过；
- `ADC_OVERRANGE`、`FIFO_OVERFLOW`、旧 config、缺片、重复冲突或超时帧没有进入分析；
- 接收、分析、发布和 UI 使用相同 session/config/epoch 身份；
- 正式 measurement 至少包含 H1 与一条合法谐波。

P4 使用三帧槽和 latest-wins。未分析的旧 latest 可以被覆盖，但 `IN_USE` 不得改写；发布前必须再次确认帧仍为 current。

## 6. 可选诊断镜像

只有需要电脑被动取证时才使用镜像构建：

1. 电脑先绑定 `.4:50002`，不发送任何 CSLP 控制包。
2. 从 `.4` 到 FPGA `.2` 预热 ARP。
3. 仍由 P4 按原顺序建立唯一 session。
4. 比较主端与镜像端 CSLP 应用载荷；镜像失败不得改变主发送结果、帧所有权或正式 STATUS。

镜像只在主发送被 lwIP 接受后尝试，使用独立 pbuf、目的 MAC/IP、UDP 端口和校验和。生产构建必须保持 `CSLP_MIRROR_ENABLED=0`。

## 7. 成功判据

一次最小联调至少满足：

- FPGA 与 P4 的启动身份、地址和 Profile 一致；
- 完成 HELLO、CONFIG、ENABLE 和首个完整帧；
- P4 产生合法 measurement，UI 从等待态进入 LIVE；
- 来源、CRC、session、config、分片、OTR、FIFO 和 socket 错误计数没有异常增长；
- 日志和结果绑定到本次构建，不借用其他 BIN 或历史会话的 PASS。

需要形成发布结论时，不能停在烟测。按[测试与证据索引](../测试与证据索引.md)选择与目标身份对应的完整矩阵和证据根。

## 常见失败

| 现象 | 先检查 |
| --- | --- |
| 持续 HELLO timeout | FPGA 是否已经监听 `.2:50000`、地址角色是否冲突、PHY 是否 Link Up |
| CONFIG 被拒绝 | Profile 六项固定值与 `max_udp_payload` 是否一致 |
| 有包但没有完整帧 | 12 分片、来源、CRC、session、config 和 50 ms assembling 超时 |
| 会话在线但 UI 为 STALE | 是否到达新的 current 有效 measurement；STATUS 不能续期 |
| 只检测到一条谱线 | 输入是否符合「H1 + 合法谐波」模型、幅值是否越过门限、FFT 内核是否为 ANSI |
| FPGA 在线但 P4 数值异常 | 校准身份、S16 位序、offset binary、OTR/FIFO 和逐频 Profile |
| 镜像丢包但主端正常 | 镜像 ARP、独立 pbuf 和镜像计数；不得据此判主链失败 |

更细的端侧检查见 [FPGA 端联调指南](../系统补偿方案/ESP32P4-FPGA联调指南-FPGA端.md)和 [ESP32-P4 端联调指南](../系统补偿方案/ESP32P4-FPGA联调指南-ESP32P4端.md)。
