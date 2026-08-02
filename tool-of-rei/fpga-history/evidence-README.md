# FPGA 原始证据档案索引

> 本目录被 Git 忽略。它保存最终 FPGA 基线的真实 ADC、前端、LAN 和固化材料；合并分支前必须做外部只读备份。唯一最终 FPGA 身份为 `fpga-v1.0.0@038e981`。

## 正式主证据

| 目录 | 简介 | 快速入口 |
|---|---|---|
| `m10-lan-selftest-20260731/` | 采集/时序/DMA/LAN 基线。 | README、formal artifacts |
| `m8-lan-longrun-20260731/` | 10,001帧真实 ADC LAN 长稳。 | README、`SHA256SUMS` |
| `m11-real-frontend-20260731/` | 真实前端、校准、FIR、干扰、组合和J长稳主链。 | README、`offline/*summary*/`、`offline/calibration-v1/` |
| `adc-direct-wire-20260731/` | ADC 接线、位序、raw-IOB 和 ORA。 | README、capture manifest |
| `m12-dual-destination-20260801/` | P4主发 + 电脑被动镜像双目的地 UDP。 | README、`SHA256SUMS` |
| `m14-qspi-persistent-20260801/` | `038e981` QSPI冷启动。 | README、`qspi_cold_boot_summary.json`、`SHA256SUMS` |

## 历史校准与诊断材料

| 目录 | 说明 |
|---|---|
| `m11-calibration-contract-20260731/` | 旧校准身份契约与构建门。 |
| `m7-adc-calibration/` | 历史 ADC 标定；不要用它覆盖 M11/最终标定。 |

## 历史/失败/诊断

- `m9-spi-preflight-20260731/`：已退出产品边界的 SPI 诊断历史；
- `adc-reconnect-*`：时序与恢复诊断；
- 各根内的 `r1`、`failed`、`old`、`diagnostic`：修复前或失败原始记录，必须保留但不可作为最终 PASS。

## 数据说明与校验

- `pcap`：网络线上包与 checksum/分片证据；
- `npy/s16le`：原始示波器或码样点；
- `summary/manifest/json`：结构化结论和输入条件；
- `SHA256SUMS`：同根完整性清单；
- `.bit/.xsa/.elf/BOOT.BIN`：必须和具体 commit/Profile 绑定。

进入含清单的根目录后执行 `sha256sum -c SHA256SUMS`。不要编辑已归档的 JSON 中绝对路径；可移植导航写在 [../../docs/验收证据索引.md](../../docs/验收证据索引.md)。
