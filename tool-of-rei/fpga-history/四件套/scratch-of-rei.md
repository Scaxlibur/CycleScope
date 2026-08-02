# scratch-of-rei

## 临时想法

- 2026-07-31 M11正式网络由用户确认；最新preflight为DG OFF/50 Ω、RTM双ACL高阻、
  DP832只读正常。`scope status`因外置RTM插件缺`scope.snapshot`在连接前能力门禁失败，
  没有裸SCPI。需现场确认DCL/探头倍率和feedback/R值后才能解锁首个20 mV点。

- 2026-07-30 23:06 重接诊断：USB/JTAG 目标完整在线，但 ARP/CSLP 完全无响应。新版接线物理脚与当前 bit 14/14 不同，先解决 XDC/易失镜像再谈 ADC 数据；不要因 JTAG 能看到 xc7z010 就误报 LAN 正常。
- 2026-07-30 14:35 停点：p210 r1/r2/r3 是 DG OFF 后误捕，已封存；协调重跑 r4/r5/r6 均在 ON 窗口且相位判据通过。正式镜像和 LAN 已恢复验证，ADC 交还他人，禁止继续 live 动作。下次从驾驶舱恢复，不要凭记忆猜接线或板上镜像。
- 10,000 帧前可把 pcap 分析整理成正式工具，避免继续使用一次性解析命令。
- SPI 实板测试应先读取 generation、长度和状态，再核对首尾样点；代号变化立即丢弃并重试。
- raw-IOB 四相位 identity 结果：270°/300°/330°/345°的普通 RMSE `114.859/84.222/55.244/4.073 code`、1% 截尾 RMSE `17.387/7.812/3.323/0.742 code`；稳健 p-p 约 `754 code`，匹配 RTM 约 `1.83 Vpp`。345°只剩样点 235 的 `0x7FD→0xA00→0x804`，不能因为 `|residual| > 512` 计数为零就漏掉这个恰为 `+512 code` 的 D9 混码。分析器早期四线交换的约 `2480 code` 是量程不可能的伪优解。
- 300°多周期出现 `0x7FE→0xFFF→0x804`，是跨中点“新 MSB＋旧低位”的直接混码证据；任意 permutation 都无法改变 `0x000/0xFFF`，所以不能靠换线表修复。

## 失败尝试

- 2026-08-01 首次在受限沙箱内生成FSBL时，旧Vitis loader需要写
  `~/.Xilinx/Vitis/2024.2`而无法创建配置区，未产生ELF、未连接硬件；按用户已授权的
  沙箱外执行后成功生成。Flash结束后的第一次易失恢复误用`xsct`入口，现有下载脚本的
  版本字符串门禁拒绝且未连接硬件；改用同套2025.1的`xsdb`后一次完整恢复成功。

- 2026-07-31 首个G pilot的采集/分析本身PASS，但自加的USER→SIN恢复调用被WaveBench
  fixed-wave快照门拒绝，点级按设计FAIL；随后发现同一插件连第二次`arb_load`也拒绝
  USER快照，且均在新上传前停止。旧FAIL点和两次错误日志保留。没有裸SCPI或修改
  WaveBench仓库；最终在tool-of-rei实现版本1.1.0＋源码SHA绑定扩展，只允许已确认
  USER/OFF进入原上传事务，失败仍由原驱动强制OFF并声明旧volatile不可恢复。独立pilot
  与余下9点全部PASS。
- 2026-07-31 F阶段23个live点均PASS后，首轮最终汇总沿用了E校准专用的固定1 kHz
  FFT峰值门，错误拒绝了200 us窗下偏离最近5 kHz bin超过1 kHz、但仍位于半bin内的
  合法F点。没有再次操作仪器；保留E默认门，F汇总改为最近半bin门并从原始NPY重算
  已知频率拟合，按相干2%/非相干10%交叉校验。63项M11测试和最终汇总均PASS。
- 2026-07-31 M11 首轮 `zero-live` 的仪器采集和 65 帧 LAN 均通过，但归档器用未经
  清洗的 `+0800` 标签匹配 WaveBench 已清洗成 `_0800` 的目录名，导致点级门禁按设计
  FAIL。原始失败证据保留在
  `evidence/m11-real-frontend-20260731/points/20260731_181554_355768+0800_b-zero-wide/`。
  已改为使用服务返回的精确package路径并要求“本轮新建＋位于data/raw”，独立重跑PASS。
- 2026-07-31 首轮 ARB dry-run runner 把相对 output 传给 cwd 位于 WaveBench 根的
  子进程，导致 payload 查错位置；没有仪器 I/O。失败目录 `offline/arb-dry-run-v1/`
  原样保留。路径已在子进程前绝对化，成功轮使用独立 `arb-dry-run-v2/`。
- 2026-07-31 更新M11当前停点会改变被matrix manifest冻结的计划哈希。已生成
  `matrix-v2`，并在runner中增加public方案/M11计划/FIR系数三哈希stale门禁；最终
  20/20单测与`arb-dry-run-v4`通过。v1/v2/v3均保留作审计，不作为最终闭合链。

- Vitis 2025.1 的 `COUNTS_PER_SECOND` 宏缺括号；直接强转宏导致时间慢 4 倍。
- AMD lwIP 的 `#cmakedefine IP_FRAG 0` 实际生成 `#undef`，而 upstream 默认值是 1。
- 仅把 `netif.mtu` 改成 1500 会让 GEM DMA 把 1514-byte 帧截成 1500 byte 且仍返回成功；必须同时修正生成的 `xemacpsif_dma.c`。
- WaveBench source restore 对初始 HARM 状态调用受限 `set_func(HARM)` 会失败；实机按 fail-safe 留在 OFF。后续计划必须显式 OFF 收尾，不能把 basic restore 当作完整 profile 恢复。
- 并行运行 p270/p330 Vivado 时共享 `.Xil/HWH/design.hdf`，330°首次在所有时序门禁通过后以 Error 139 崩溃；串行重跑完整通过，后续系统构建禁止并行。

## 待提升到项目快照/已知问题的内容

- 取得多次复现的无混码相位并用现场接线核对或 DC/偏置慢斜坡确认 `A10/A11` 后，冻结完整位序并缩减对应已知问题。
- 有硬件时间戳或交换机镜像口后，补做 500 us 分片起点间隔的严格线级证明。
