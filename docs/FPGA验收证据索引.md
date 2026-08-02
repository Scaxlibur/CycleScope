# CycleScope FPGA 验收证据索引

更新：2026-08-02
范围：codex/FPGA 冻结基线 fpga-v1.0.0@038e981。本文是主仓的可移植导航；原 FPGA 索引原文保存在 tool-of-rei/fpga-history/FPGA验收证据索引-原文.md。

## 正式 FPGA 证据根

| 证据根 | 已验证范围 | 原始归档位置 |
|---|---|---|
| M10 LAN 自检 | 采集、时序、DMA、故障注入和真实 ADC LAN 基线。 | CycleScope-FPGA/tool-of-rei/evidence/m10-lan-selftest-20260731/ |
| M8 LAN 长稳 | 10,001 帧真实 ADC、120,012 个 WAVE 包、UDP checksum、零 IP 分片与连续性。 | CycleScope-FPGA/tool-of-rei/evidence/m8-lan-longrun-20260731/ |
| M11 真实前端 | AD8065/AD9226、FIR、动态范围、频响、干扰、组合波和 J 长稳。 | CycleScope-FPGA/tool-of-rei/evidence/m11-real-frontend-20260731/ |
| ADC 直连 | AD9226 位序、raw-IOB 相位、ORA 和码域基础证据。 | CycleScope-FPGA/tool-of-rei/evidence/adc-direct-wire-20260731/ |
| M12 双目的地 UDP | P4 主链与电脑被动镜像，100/1,000 帧逐字节载荷对照。 | CycleScope-FPGA/tool-of-rei/evidence/m12-dual-destination-20260801/ |
| M14 QSPI 固化 | fpga-v1.0.0@038e981 的 PS+PL 写入与掉电冷启动。 | CycleScope-FPGA/tool-of-rei/evidence/m14-qspi-persistent-20260801/ |

M11 中的 calibration-v1 是历史 FPGA 校准复现链；ESP32-P4 运行时使用的最终频响资产仍以主仓的 final-calibration-20260801_145546+0800/fit-v2 和 p4-asset-v2 为准，两者不能混用。

## 保存与复核

1. 原始 pcap、npy、s16le、仪器日志、bit/XSA/ELF 和根 SHA256SUMS 均留在 FPGA 本机/外部归档，不随普通 Git 合并。
2. 含 SHA256SUMS 的根应在原根目录执行 sha256sum -c SHA256SUMS；不得重命名、编辑或重采后覆盖。
3. 本次仅抽查验证了 M8、M9、M11 calibration contract、M12 和 M14 的根清单；M11 主根的逐包清单必须与原目录一起保存。
4. FPGA 历史四件套、证据分类 README、LAN 回放源说明与 M11 夹具源码已在主仓按命名空间归档，见 tool-of-rei/fpga-history/ 和 tool-of-rei/test/fpga-m11/。

全工程的 P4 最终标定、M8/M9 20 例审计和 F0 显示版边界见 [验收证据索引.md](验收证据索引.md)。
