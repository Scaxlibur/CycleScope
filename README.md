# CycleScope

CycleScope 是面向全国大学生电子设计竞赛 G 题的周期信号测量分析装置。系统由模拟前端、AD9226、Zynq-7010 和 ESP32-P4 组成：FPGA 完成采集、滤波与抽取，Zynq PS 通过 CSLP/UDP 发送连续帧，ESP32-P4 完成 FFT、频响补偿、时域/频域投影和触摸显示。

> [!WARNING]
> JTAG 下载、P4 烧录、串口打开、网络重放和信号源操作都可能改变真实设备状态。首次接手应从[安全构建与上板](docs/getting-started/安全构建与上板.md)的离线步骤开始；没有新的硬件操作授权时，只执行离线验证和构建。

## 系统数据流

```text
BNC 输入
  → 模拟调理与 AD9226 采样
  → Zynq PL 三级 FIR / 16 倍抽取
  → Zynq PS DMA / CSLP UDP
  → ESP32-P4 FFT / 补偿 / 1P、3P 投影
  → 1024×600 LVGL 触摸显示
```

完整参数与职责见 [G 题采样与处理 Profile](docs/协议与接口/CSLP-G题采样与处理-Profile-v0.1.md)，线上报文合同见 [CSLP UDP 通信协议](docs/协议与接口/CSLP-UDP-通信协议-v0.1.md)。

## 当前边界

- 正式普通输入范围不超过 250 mVpp；450 mVpp 已确认存在模拟前级压缩，只保留为历史负证据。
- M12 工程联调、M8/M9 物理审计和 F0 显示镜像分别绑定不同证据范围，不能互相替代。
- TIME、FFT、1P、3P 按键到真实面板完成刷新的 2 秒时限，以及整机单路 5 V、屏幕和 BNC 实物条件，仍需现场留证。

精确构建身份、误差和「能证明／不能证明」范围只在[测试与证据索引](docs/测试与证据索引.md)维护，本页不复制完整哈希和审计表。

## 安全开始

[安全构建与上板](docs/getting-started/安全构建与上板.md)提供一条线性路径：

1. 运行不接触板卡的主机测试和离线检查；
2. 构建 PL、PS 与 ESP32-P4 产物并核对配置；
3. 先执行 JTAG dry-run；
4. 取得明确授权并复核接线后，才执行真实下载和烧录。

## 仓库结构

| 路径 | 职责 |
| --- | --- |
| [`Zynq_7010_PL/`](Zynq_7010_PL/README.md) | AD9226 接口、FIR、抽取、AXI-Stream/DMA 集成与 Vivado 构建 |
| [`Zynq_7010_PS/`](Zynq_7010_PS/cyclescope_cslp/README.md) | CSLP 控制、帧发送、网络诊断与 Vitis 构建 |
| `ESP32-P4/` | CSLP 接收、FFT、补偿、投影与 LVGL 显示 |
| `docs/` | 当前协议、架构、操作、标定和证据导航 |
| `final_doc/` | 设计报告 Markdown 源和独立排版的 HTML 预览 |
| `source_data_for_test/` | 本机/外部回放归档；普通 Git clone 只包含说明文件 |

## 文档入口

| 目标 | 入口 |
| --- | --- |
| 按任务查找文档 | [文档首页](docs/README.md) |
| 接手项目并恢复上下文 | [项目交接资料索引](docs/项目交接资料索引.md) |
| 安全验证、构建与上板 | [安全构建与上板](docs/getting-started/安全构建与上板.md) |
| 查询测试、镜像与证据边界 | [测试与证据索引](docs/测试与证据索引.md) |
| 复核详细证据根 | [验收证据索引](docs/验收证据索引.md) |
| 查询 FPGA 冻结基线 | [FPGA 验收证据索引](docs/FPGA验收证据索引.md) |
| 阅读最终设计报告 | [设计报告交付目录](final_doc/README.md) |
| 维护文档 | [CycleScope 文档规则](docs/文档规则.md) |

赛题、答疑、厂商资料和历史设计稿保留在 `docs/`，但不作为当前实现或发布事实源。已经清单化的原始证据不得因文档整理而改名、移动或改写。

## 许可证与致谢

本项目采用 [The Unlicense](LICENSE)。感谢 Linux DO 社区提供交流与支持。
