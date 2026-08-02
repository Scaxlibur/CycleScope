# FPGA tool-of-rei 驾驶舱

此目录是 `codex/FPGA` worktree 的本地恢复与证据档案，不随 Git 合并自动传递。可跟踪的交接入口是 [../docs/验收证据索引.md](../docs/验收证据索引.md)。

| 位置 | 内容 | 当前整理规则 |
|---|---|---|
| `任务清单.md` | 最终基线、回归和交接动作。 | Now 只能有一个当前任务；历史结论转入证据索引。 |
| `项目快照.md` | 分支、硬件安全、构建和主要证据状态。 | 顶部恢复卡必须明确唯一最终基线 `fpga-v1.0.0@038e981`。 |
| `已知问题.md` | 真正尚未关闭的风险。 | 不把外部时序模型、人工整机验收和归档风险藏进历史段落。 |
| `scratch-of-rei.md` | 临时分析。 | 完成后提炼，不承担正式结论。 |
| `evidence/` | 1GB级原始仪器、LAN、校准和 QSPI 证据。 | 见 [evidence/README.md](evidence/README.md)，保持原名与 SHA。 |
| `source_data/` | 原始 pcap、样点和 WaveBench 归档。 | 与 evidence 的点级分析互补，不能擅自去重删除。 |
| `m11/`、一次性脚本 | 现场 runner/私有配置/验证工具。 | 需要长期复现时再提升源码；缓存和 build 不能冒充证据。 |

禁止清理 `source_data/` 或 `evidence/` 中的正式根；构建缓存可以重建，但不得替代已归档证据。每次恢复先读本 README、四件套和证据索引，再接触板卡、QSPI 或 MIO47。
