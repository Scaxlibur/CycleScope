# CycleScope-main 原始证据档案索引

> 本目录被 Git 忽略。它是原始测量和运行数据的本地/外部归档，不是可以随分支 merge 自动传递的文件夹。

状态解释：正式表示可作为对应范围的最终结论入口；辅助表示被正式审计引用或支撑；历史/负例表示必须保留但不能写成 PASS。任何状态都不授权修改原始包。

## 正式优先证据

| 目录 | 简介 | 关键入口 |
|---|---|---|
| `final-calibration-20260801_145546+0800/` | 真实前端最终标定、7 点独立 holdout 和 P4运行时频响资产。 | `fit-v2/`、`holdout-v2/`、`p4-asset-v2/`、根 `SHA256SUMS` |
| `final-p4-m8m9-fixed-20260801_184849+0800/` | M8/M9固定版实机案例原始包。 | `campaign-summary.json`、`campaign-manifest.json`、`SHA256SUMS` |
| `final-p4-wdt-recheck-20260801_184750+0800/` | 看门狗修复后再验证。 | `campaign-summary.json`、`SHA256SUMS` |
| `final-p4-release-audit-20260801_190121+0800/` | 20 例合并审计，`pass=true`。 | `combined-audit.json`、`SHA256SUMS` |
| `final-p4-f0-display-20260801_192900+0800/` | F0 两位小数显示构建、启动及100帧链路烟测。 | `README.md`、`SHA256SUMS`、`startup.log` |
| `m12-*/` | 真实 DG→FPGA→LAN→P4 的60例矩阵及镜像采集。 | `m12-final-completion-audit.json` 与被其引用的全部子包。 |

## 辅助与历史材料

| 目录 | 定位 |
|---|---|
| `final-p4-m8m9-20260801_181529+0800/` | 最终固定版前的 M8/M9 原始运行包；审计会引用，继续保留。 |
| `final-p4-m8m9-edge-20260801_183431+0800/` | 500 kHz 修复前边界/负例；不是完整 PASS。 |
| `final-p4-release-audit-20260801_185829+0800/` | 仅 `INCOMPLETE.txt`，保留失败原因，不能作最终验收。 |
| `m12-campaign-dryrun-*`、`m12-runner-dry-*` | 无仪器/联调前的流程验证。 |
| `failed-attempts/`（在最终标定根内） | 原始失败、停机和排除记录；不可删除或覆盖。 |

## 数据格式速览

- `pcap` / `pcap-analysis.json`：线上分片、checksum、IP 分片和抓包丢失证据。
- `selected-frames-s16le.npy`、帧 JSON：FPGA/P4 实际码样点与重组边界。
- `*.npy`、WaveBench raw：示波器原始采集，不能用截图替代。
- `campaign-*.json`、`manifest.json`、`summary.json`：输入条件、版本、结果和 SHA 的结构化入口。
- `.elf/.bin/sdkconfig/CMakeCache`：固件身份；必须和 UART 启动日志、读回/烧录记录对应。

## 校验与外部备份

在某个根目录中执行：

```bash
sha256sum -c SHA256SUMS
```

若根目录不含 `SHA256SUMS`，先阅读其 README/manifest，不要自行重算后覆盖旧事实。离线备份必须保留目录名和层级；`m12` 主审计需要与其引用的原始子包一起保存，单独复制一个 JSON 没有复现意义。

详细的合并边界、FPGA 对照和发布身份见 [../../docs/验收证据索引.md](../../docs/验收证据索引.md)。
