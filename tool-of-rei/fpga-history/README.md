# FPGA 历史驾驶舱与归档说明

本目录把原 FPGA 工作树中未受 Git 跟踪、但对追溯开发过程有价值的轻量材料按命名空间保留，避免覆盖 main 的四件套历史。

## 来源与身份

- 来源工作树：CycleScope-FPGA
- 来源分支：codex/FPGA
- 冻结基线：fpga-v1.0.0@038e981
- 导入原因：Git 合并只带入受版本控制的 FPGA 源码；原 FPGA 四件套、证据索引、M11 夹具和大体积档案原先均被忽略。

## 本次版本化的轻量材料

| 位置 | 内容 |
|---|---|
| 四件套/ | FPGA 的任务清单、项目快照、scratch 和已知问题原文，完整保留而不与 main 同名文件互相覆盖。 |
| FPGA-worktree-README.md | 原 FPGA 驾驶舱说明。 |
| FPGA验收证据索引-原文.md | 原 FPGA 专用验收索引原文；面向主仓的可移植入口见 ../../docs/FPGA验收证据索引.md。 |
| evidence-README.md | FPGA 原始证据根分类说明。 |
| source_data-README.md | FPGA 原始 LAN 重放源分类和隔离网络警告。 |
| M11-真实全链路FIR与信号处理压力测试计划.md、开发里程碑.md、plans/ | 历史计划与里程碑。 |

可复现的 M11 runner、离线分析和单测源码已归并到 ../test/fpga-m11/；它们仍可能访问真实仪器，运行前必须先阅读其中 README。

## 未纳入普通 Git 的原始档案

以下材料已原样复制到当前 main 工作树的忽略归档区，同时继续保留在 CycleScope-FPGA 工作树；绝不因本次合并删除或移动：

| 原位置 | 规模与定位 |
|---|---|
| tool-of-rei/evidence/ | 当前 main 的 tool-of-rei/evidence/ 中约 1.2 GB、30,000 余文件；含 M8/M10/M11/M12/M14、ADC 接线与恢复证据。 |
| tool-of-rei/source_data/ | 当前 main 的 source_data_for_test/ 中约 256 MB、124 个点级 pcap 根，每点有 SHA256SUMS。 |
| data/ | 当前 main 的 tool-of-rei/evidence/fpga-wavebench-raw-20260731/ 中约 1.4 MB 的两次 100 kHz/1 Vpp WaveBench 原始辅助包。 |
| references/ | 约 165 MB 的旧 Vivado 参考工程与 bitstream，不是当前正式验收根。 |
| tool-of-rei/private/ | 本地仪器配置，含地址信息；明确禁止提交。 |

抽查时 M8、M9、M11 校准契约、M12 和 M14 的根 SHA256SUMS 均已通过。M11 主根没有单一总清单，内部保留 351 份逐包清单。删除或清理 FPGA 工作树前，必须将这些忽略大包原样备份。
