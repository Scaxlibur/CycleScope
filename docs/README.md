# CycleScope 文档

本页按任务组织 CycleScope 的维护型文档。首次接手可从[安全构建与上板](getting-started/安全构建与上板.md)开始；需要判断量化结论或构建身份时，直接进入[测试与证据索引](测试与证据索引.md)。

> [!WARNING]
> 文档中的 JTAG、烧录、串口、网络和仪器命令不等于操作授权。默认只执行离线验证；真实设备操作必须先确认接线、输入范围、输出关闭状态、隔离网络和目标身份。

## 第一次接手

- [安全构建与上板](getting-started/安全构建与上板.md)：从离线验证到构建、dry-run 和授权后的上板操作。
- [项目交接资料索引](项目交接资料索引.md)：按维护、复现和答辩目标恢复上下文。
- [项目首页](https://github.com/Scaxlibur/CycleScope#readme)：了解系统用途、数据流和当前边界。

## 构建与维护

- [FPGA 与 ESP32-P4 联调](how-to/FPGA与ESP32-P4联调.md)：两板主链的最短启动、验证和诊断顺序。
- [Zynq PL 构建说明](https://github.com/Scaxlibur/CycleScope/blob/main/Zynq_7010_PL/README.md)：Vivado 仿真、综合、系统 XSA 与 bitstream。
- [Zynq PS 构建说明](https://github.com/Scaxlibur/CycleScope/blob/main/Zynq_7010_PS/cyclescope_cslp/README.md)：CSLP 主机测试、Vitis 构建和 LAN 边界。
- [ESP32-P4 端联调](系统补偿方案/ESP32P4-FPGA联调指南-ESP32P4端.md)：正式配置、fresh build、烧录与启动日志。
- [FPGA 端联调](系统补偿方案/ESP32P4-FPGA联调指南-FPGA端.md)：PL/PS 数据流、诊断和最低联调流程。

## 协议与架构

- [G 题采样与处理 Profile](协议与接口/CSLP-G题采样与处理-Profile-v0.1.md)：项目固定采样参数、电压语义和系统边界。
- [G 题采集与分析架构](concepts/G题采集与分析架构.md)：抽取、谐波分析、帧所有权和任务分工的设计原因。
- [CSLP UDP 通信协议](协议与接口/CSLP-UDP-通信协议-v0.1.md)：线上字节格式、状态机、错误与恢复合同。
- [AD9226 通道 A 与 Zynq-7010 接线](协议与接口/AD9226通道A与Zynq-7010接线定义.md)：板级连接、位序和约束映射。
- [FPGA 双目的地 UDP 设计](协议与接口/FPGA双目的地UDP发送设计规划.md)：默认关闭的诊断镜像设计与历史实施记录。

## 标定与安全

- [系统补偿方案](系统补偿方案/README.md)：当前补偿、模拟前端和两板联调入口。
- [前端增益与逐频补偿](系统补偿方案/前端增益与逐频补偿指南.md)：标定身份、插值边界和变更纪律。
- [模拟前端调试注意事项](系统补偿方案/模拟前端放大器调试注意事项.md)：量程、接线、OTR 与停机条件。

## 证据与报告

- [测试与证据索引](测试与证据索引.md)：当前范围、证据等级、构建身份和不能证明的内容。
- [验收证据索引](验收证据索引.md)：详细原始根、哈希、失败记录和使用纪律。
- [FPGA 验收证据索引](FPGA验收证据索引.md)：`fpga-v1.0.0` 的冻结基线。
- [轻量证据摘要](evidence/README.md)：随 Git 保存的摘要和清单；不能替代原始档案。
- [最终设计报告](https://github.com/Scaxlibur/CycleScope/blob/main/final_doc/README.md)：报告正文、HTML 预览和引用纪律。

## 来源与历史

以下材料用于追溯，不参与当前实现或发布结论的优先级竞争：

- [赛题原件与历史初稿](https://github.com/Scaxlibur/CycleScope/tree/main/docs/G题_周期信号测量分析装置)
- [主办方答疑](https://github.com/Scaxlibur/CycleScope/tree/main/docs/主办方答疑)
- [Profile v0.1 实施与验收记录](history/CSLP-G题-Profile-v0.1-实施与验收记录.md)
- [WaveBench 使用反馈](工具反馈/WaveBench使用体验与优化建议清单.md)：CycleScope 标定过程形成的外部工具改进记录，不是当前产品说明。
- `docs/AD9226_2CH模块使用手册_V1.0/`
- `docs/ESP32-P4-Function-EV-Board v1.4 - ESP32-P4 - — esp-dev-kits latest 文档-0e31eb87-16e0-4fc4-9f05-805a80640e07/`
- `docs/Z7-Nano 用户手册 — 微相科技FPGA用户手册 V1.0 文档-11c1ee4a-297f-4d5d-85f3-7d32378c2531/`
- 标为 Historical 的旧计划、诊断记录和设计稿

## 文档维护

[CycleScope 文档规则](文档规则.md)定义页面职责、事实源、状态、证据不可变边界和迁移动作。结构与事实确定后再处理中文表达；协议字段、路径、哈希、外部原件和机器文件保持原样。

本地检查和 CI 阻断规则见[文档工作流](development/文档工作流.md)。
