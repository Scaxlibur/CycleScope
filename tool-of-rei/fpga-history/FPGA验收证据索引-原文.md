# CycleScope FPGA 合并前验收证据索引

> 更新：2026-08-02
> 本文件是 `codex/FPGA` 工作树的可追踪导航；原始证据保持在被忽略的 `tool-of-rei/evidence/` 与 `tool-of-rei/source_data/`。

## 1. 归档与复核规则

1. 不删除、不改名、不重写已带 `SHA256SUMS` 的证据根，也不把新结论反写进历史原始日志。
2. 备份时必须同时保存本索引、每个根的 README/summary/manifest、`SHA256SUMS` 和引用的原始 `pcap/npy/s16le`。
3. 在证据根执行 `sha256sum -c SHA256SUMS`。原始 JSON 中的绝对路径属于历史记录，不能为“可移植性”改写；可移植说明写在本索引中。
4. 证据的结论只对其 bit/ELF、Profile、校准身份和现场条件成立。FPGA 唯一最终基线是 `fpga-v1.0.0@038e981`；不得把其他源码或构建物混入该身份。

## 2. 正式基础与真实前端证据

| 证据根 | 已验证范围 | 首选入口 |
|---|---|---|
| [M10 LAN自检](../tool-of-rei/evidence/m10-lan-selftest-20260731/) | 正式 bit/ELF 身份、实现时序、DMA、故障注入和真实 ADC LAN 基线。 | README、`artifacts/formal/`、summary/manifest |
| [M8 LAN长稳](../tool-of-rei/evidence/m8-lan-longrun-20260731/) | 10,001 帧真实 ADC、120,012 WAVE 包、UDP checksum、零 IP 分片、帧率与数据连续性。 | README、`SHA256SUMS` |
| [M11真实前端](../tool-of-rei/evidence/m11-real-frontend-20260731/) | AD8065/AD9226、FIR、动态范围、频响/校准、干扰抑制、组合波和 J 长稳主证据。 | README、`offline/*-summary-v1/summary.json`、`offline/calibration-v1/` |
| [ADC直连](../tool-of-rei/evidence/adc-direct-wire-20260731/) | AD9226 位序、raw-IOB 相位、ORA 和真实码域基础证据。 | README、capture manifest |

M11 的 `offline/calibration-v1/` 只是一条历史 FPGA 校准复现链；P4 当前运行时频响资产应以相邻主工程的 `final-calibration-20260801_145546+0800/fit-v2` 与 `p4-asset-v2` 为准。两者不能互相替代。

## 3. 通信、镜像与固化证据

| 证据根 | 范围 | 使用边界 |
|---|---|---|
| [M12 双目的地UDP](../tool-of-rei/evidence/m12-dual-destination-20260801/) | 主发后 best-effort 镜像、100帧功能轮、1,000帧稳定轮、pcap逐字节载荷对照。 | 电脑仅被动接收；P4 是唯一 CSLP 控制端。 |
| [M14 QSPI固化](../tool-of-rei/evidence/m14-qspi-persistent-20260801/) | `fpga-v1.0.0@038e981` 的 PS+PL QSPI 写入和冷启动。 | 唯一最终 FPGA 基线的掉电恢复证据；固定 8192 点。 |

## 4. 历史/诊断材料

| 类别 | 说明 |
|---|---|
| `m7-adc-calibration`、`m9-spi-preflight` | 历史 ADC/SPI 诊断，保留以说明演进，不能作为最终 LAN 数据路径的 PASS。 |
| `adc-reconnect-*` | 断电、时序和恢复过程的诊断记录。 |
| M11 中带 `failed`、`r1` 或旧重分析后缀的目录 | 失败/修复前原始证据；不可删除或由重跑结果覆盖。 |
| `build/`、`.Xil/`、Vitis/Vivado 临时输出 | 可重建输出；不能冒充 M8/M10/M11/M12/M14 的归档证据。 |

## 5. 原始数据分类

| 数据 | 内容 | 备份要求 |
|---|---|---|
| `pcap` / pcap analysis | UDP checksum、IPv4 分片、接口丢包与数据报数量。 | 连同 tcpdump 日志和应用统计保留。 |
| `*.npy` / `*.s16le` / frame JSON | 示波器、ADC、DMA/CSLP样点；用于离线重分析。 | 原样保留，禁止重采覆盖。 |
| `summary.json` / manifest / `SHA256SUMS` | 结论、输入条件、资产身份和完整性。 | 与根目录一起备份。 |
| `.bit/.xsa/.elf/BOOT.BIN` | 可下载发布产物。 | 必须关联 commit、Profile、构建工具版本和 SHA。 |
| 仪器/JTAG日志 | 安全输出、供电只读、烧录、冷启动和恢复。 | 失败日志亦必须保留。 |

跨工程的 P4 最终校准和 20 例审计见 [CycleScope-main/docs/验收证据索引.md](../../CycleScope-main/docs/验收证据索引.md)。
