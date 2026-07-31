# SPI 诊断接口上板接线与验收

> 历史文档：项目已于 2026-07-31 收敛为 LAN-only。本文件仅保留范围变更前的
> 设计记录；SPI 不再启用、接线、测试、验收或维护，也不等待 USB-SPI 适配器。
> 当前及后续正式验证一律通过电脑端 CSLP/UDP over LAN 完成。

## 1. 边界

SPI 是冻结 BRAM 的只读诊断旁路，不是正式数据链。正式链始终为
`AXI DMA → PS → CSLP/UDP`；SPI 没有反压采样、DMA 或 UDP 的权利。

当前协议固定为 mode 0、MSB-first、3.3 V 逻辑、`SCLK ≤ 5 MHz`。不得为了寻找
“极限”超过 5 MHz，因为 5 MHz 是契约上限，不是建议值。

## 2. Z7-Nano JP1 接线

使用独立的 3.3 V USB-SPI 主机，并与 Z7-Nano 共地：

| USB-SPI 主机 | 方向 | Z7-Nano JP1 | Zynq 封装脚 | PL 端口 |
|---|---|---:|---|---|
| `CS_N` | 主机 → Zynq | 40 | `R14` | `spi_cs_n` |
| `SCLK` | 主机 → Zynq | 38 | `T10` | `spi_sclk` |
| `MOSI` | 主机 → Zynq | 34 | `W13` | `spi_mosi` |
| `MISO` | Zynq → 主机 | 36 | `T15` | `spi_miso` |
| `GND` | 共地 | 30 或 12 | - | - |

不要连接 JP1 的 5 V。若适配器自身供电，只接四根信号线和 GND；如适配器需要
目标电压参考，只能按其手册接 JP1-29 的 3.3 V reference，不得向板卡反向供电。

`R14` 同时连接板载低有效 LED D3，因此 `CS_N` 拉低时 LED 可能点亮，这不是协议
错误。拉低 `CS_N` 后应保持 `SCLK=0` 至少 100 ns，再发出第一个上升沿。

本机当前的 `0403:6014` 是正在承担 JTAG 的 Digilent FT232H，不得停止
`hw_server` 后抢占为 SPI；`CP2102N` 是 ESP32-P4 的 UART 桥，也不是 SPI 主机。
M9 需要另接一个独立 USB-SPI 适配器，且不能触碰并行开发中的 ESP32-P4。

## 3. 协议

### `0xA0 GET_INFO`

主机发送 `A0` 后再发送 10 个 dummy byte，忽略命令字节同时收到的值。后 10 byte：

| 偏移 | 长度 | 含义 |
|---:|---:|---|
| 0 | 2 | ASCII `CS` |
| 2 | 1 | 版本，固定 1 |
| 3 | 1 | STATUS 低 8 bit 快照 |
| 4 | 4 | 非零 `frame_id`，大端 |
| 8 | 2 | 样点数，大端，固定 8192 |

### `0xA1 READ_SAMPLES`

主机发送 `A1`、16 bit 大端起始样点下标、1 byte dummy，随后连续读取 S16_LE
样点。完整 8192 点事务共发送/接收 `4 + 16384` byte。

每次 `CS_N` 拉低时锁存 bank 和 generation。若事务中 generation 改变，后续数据
归零；主机必须丢弃整次事务。稳妥流程是：

1. 单独执行一次 GET_INFO，记录 `frame_id_before`。
2. 在一个连续 `CS_N` 事务中读取完整 8192 点。
3. 再执行 GET_INFO，记录 `frame_id_after`。
4. 只有前后 frame_id 相同、非零、长度为 8192，且 OTR/overflow/drop 为 0 时接收。
5. 在并发 UDP 捕获中找到同一个 frame_id，要求 16384 byte 样点逐字节完全一致。

SPI v0.1 没有线级 CRC。不得把“没有发现异常”写成 CRC 通过；当前完整性门禁是
前后代际一致，再与带 CSLP CRC 的同 frame_id UDP 帧逐字节比较并记录 SHA-256。

## 4. 时钟验收

GET_INFO/短读依次测试 `100 kHz、500 kHz、1 MHz、2 MHz、4 MHz、5 MHz`，每档
至少 1000 次，要求 magic/version/长度始终正确且 frame_id 单调非零。

完整 8192 点只在 4 MHz 和 5 MHz 做正式门禁：纯线上时间分别约 32.8 ms 和
26.2 ms，能落在 50 ms generation 窗口内。2 MHz 完整帧至少约 65.6 ms，跨代归零
是设计预期，不能据此误判低速 SPI 损坏。

5 MHz 通过标准：至少 100 个前后代际一致的完整帧全部与同 frame_id UDP 数据
逐字节一致；不得出现随机 magic、版本、长度、半帧或未识别的非零尾部。

## 5. UDP＋SPI 并发

1. 先完成 1200 帧并发 smoke，再执行 10,000 帧正式并发。
2. SPI 以 5 MHz 周期性读取；跨代事务只计为预期 retry，不得进入有效帧计数。
3. UDP 继续要求 CRC、序号、帧号、STATUS、OTR、FIFO、drop、socket 和 NIC 错误全零。
4. 比较并发前后 PL `frames_dropped`、PS STATUS 与 GEM 错误计数；SPI 不得改变它们。
5. 原始 SPI、UDP、pcap 和主机报告统一归档到 `tool-of-rei/evidence/`。

离线解析与同帧比较由
`Zynq_7010_PS/cyclescope_cslp/tools/cslp_spi_protocol.py` 提供；具体 USB transport
必须在适配器型号确认后单独实现，不能盲猜驱动并触碰现有 JTAG/P4 设备。
