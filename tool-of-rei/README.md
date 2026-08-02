# tool-of-rei 驾驶舱与证据档案

这里是本 worktree 的恢复入口，不是普通 Git 发布目录。原始证据、截图和运行缓存仍被 `.gitignore` 排除；为保证可复现性，`test/` 下的夹具源码和 README 作为明确例外随合并提交。可合并、可引用的摘要在 [../public/测试与证据索引.md](../public/测试与证据索引.md)。

## 目录分类

| 位置 | 内容 | 保留方式 |
|---|---|---|
| `任务清单.md` | 当前唯一收口任务和合并前人工动作。 | 跟踪；只保留当前/近期结论。 |
| `项目快照.md` | 恢复卡片、构建身份、架构边界与风险。 | 跟踪；不要写成流水账。 |
| `已知问题.md` | 仍未关闭的高/中低风险。 | 跟踪；已关闭事项移到快照或证据索引。 |
| `scratch-of-rei.md` | 临时想法和失败尝试。 | 跟踪；完成后应提炼或清理。 |
| `evidence/` | 已归档的仪器、LAN、P4、校准和构建证据。 | 忽略；根 README 与 SHA 清单必须随外部备份保存。 |
| `source_data_for_test/` | 可重放的真实 ADC/pcap 输入。 | 忽略；不能因与 evidence 有关联就删除。 |
| `test/` | 标定、协议重放、故障注入与主机回归夹具。 | 源码和 README 跟踪；`__pycache__`、日志和构建产物仍忽略，见 [test/README.md](test/README.md)。 |
| `screenshots/`、`*.log` | UI 图像、串口和临时运行日志。 | 仅 [screenshots/README.md](screenshots/README.md) 跟踪；像素、相机帧和日志仍忽略。 |

## 当前优先入口

1. [../public/测试与证据索引.md](../public/测试与证据索引.md)：跨标定、审计、镜像和回放的合并入口。
2. [evidence/README.md](evidence/README.md)：按“正式、辅助、历史/负例”分类的原始数据目录。
3. [最终系统标定里程碑.md](最终系统标定里程碑.md)：标定过程的详细说明；其最终事实受 `final-calibration-...` 清单约束。
4. [../docs/验收证据索引.md](../docs/验收证据索引.md)：给合并、答辩和外部审查使用的详细索引。

## 不要做的事

- 不重命名已含 `SHA256SUMS` 的证据根，不编辑其中的历史 JSON/日志来“统一文字”。
- 不把测试 build、缓存、临时 `/tmp` 目录当成唯一证据；需要时先复制最小可验证产物并生成清单。
- 不把 `final-p4-release-audit-...` 的旧 BIN 与后续 F0 显示 BIN 混为同一发布身份。
- 不清理 `source_data_for_test`、`evidence` 或 `test`，除非已有外部归档且用户明确授权。
