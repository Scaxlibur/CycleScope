# CycleScope

CycleScope 是面向周期信号测量与谐波分析的软硬件协同项目：FPGA 采集并通过 CSLP/UDP 发送连续帧，ESP32-P4 完成实时 FFT、频响补偿、时域/频域投影和 LVGL 显示。

本仓的 main 已整合 ESP32-P4 与 FPGA 主线；原 FPGA 工作树仍作为原始证据的只读来源保留。所有原始仪器、LAN、串口和构建档案均保留在本机，不会因合并被删除、移动或改写。

## 🌟 特别鸣谢

<p align="center">
  <a href="https://linux.do">
    <img src="doc/images/linuxdo.png" alt="LINUX DO" width="420" />
  </a>
</p>
<p align="center"><b>学AI，上L站！祝小破站越来越好～</b></p>

## 当前交付边界

- 正式模拟链标定的普通输入范围冻结为不超过 250 mVpp。450 mVpp 压缩点保留为历史负证据，不参与当前拟合、保留验证或验收。
- 频响补偿资产为 Profile C5DCDE41，已形成可复核的标定摘要、独立 holdout 和 M8/M9 20 例审计。
- 提交 `7e23060` 已纳入 F0 显示到 0.01 Hz 的改动。其新镜像完成构建、烧录哈希校验及真实 FPGA 100 帧启动/链路回归；完整 M8/M9 20 例审计绑定的是较早的应用 BIN。两者不能混作同一发布身份。
- 现有 v1.1.0 标签指向当前 HEAD 之前的冻结点，不移动该标签，也不把它宣称为包含本工作树未提交的标定与 F0 显示改动。

## 从这里开始

| 需要了解什么 | 入口 |
|---|---|
| 项目交接、文档阅读顺序和用途 | [docs/项目交接资料索引.md](docs/项目交接资料索引.md) |
| 测量数据、运行数据和证据适用边界 | [docs/测试与证据索引.md](docs/测试与证据索引.md) |
| 面向合并/答辩的详细证据根索引 | [docs/验收证据索引.md](docs/验收证据索引.md) |
| 原始赛题、答疑、厂商资料和历史设计稿 | [docs/README.md](docs/README.md) |
| FPGA 冻结基线与验收证据 | [docs/FPGA验收证据索引.md](docs/FPGA验收证据索引.md) |
| 最终报告稿件 | [final_doc/README.md](final_doc/README.md) |
| 本工作树的恢复、夹具和原始归档导航 | [tool-of-rei/README.md](tool-of-rei/README.md) |

## 合并与复现原则

可提交内容包括源码、测试夹具、说明文档、轻量 JSON/CSV 摘要和 SHA256 清单。原始波形、pcap、S16 帧、ELF/BIN 以及批量截图仍作为本机/外部证据归档保存，不直接进入普通 Git。

如需复核原始档案，请先阅读 [docs/测试与证据索引.md](docs/测试与证据索引.md) 和 [tool-of-rei/evidence/README.md](tool-of-rei/evidence/README.md)；任何带 SHA256SUMS 的证据根都应原路径、原文件名保留，不要为了“整洁”重命名或编辑。
