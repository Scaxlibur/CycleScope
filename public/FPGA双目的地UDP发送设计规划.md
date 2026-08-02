# FPGA 双目的地 UDP 发送设计规划

## 1. 文档状态与目标

- 状态：PS双发、电脑被动镜像工具及`.3/.4`独立板测已完成；真实P4联合验收待执行
- FPGA实现提交：`038e981 feat(fpga): add dual-destination UDP diagnostics`
- 适用范围：CycleScope Zynq-7010 PS 端 CSLP v0.1 发送链路
- 主链路：Zynq FPGA/PS 向 ESP32-P4 发送控制响应、状态和波形
- 诊断链路：Zynq FPGA/PS 向调试电脑发送同一份 CSLP 应用数据报副本
- 目的：在不改变 FPGA 与 P4 单会话、点对点控制关系的前提下，让普通交换机上的电脑能够分析 FPGA 的出站数据

本文只规划 Zynq PS/lwIP/GEM0 上的软件双发，不增加第二个以太网 MAC、PHY 或 PL 数据通路。线上 CSLP 字节格式、状态机和主链参数继续分别以 [CSLP UDP 通信协议 v0.1](./CSLP-UDP-通信协议-v0.1.md) 和 [CSLP G题采样与处理 Profile v0.1](./CSLP-G题采样与处理-Profile-v0.1.md) 为准。

本文中的“主发送”是发往 P4 的正式发送，“镜像发送”是发往电脑的诊断副本。镜像不是第二个 CSLP 会话，也不提供第二个控制端。

## 2. 设计原则

1. P4 始终是唯一合法 CSLP 控制对端，电脑不得发送 `HELLO`、`CONFIG_SET`、`ENABLE_PUSH` 或 `DISABLE_PUSH`。
2. 主发送先于镜像发送；只有主发送被 lwIP 接受后，才尝试发送镜像副本。
3. 镜像严格为 best-effort。镜像失败、电脑掉线、端口未监听或 ARP 未解析均不得中止主帧、改变会话状态或阻塞采集。
4. 一个 CSLP 数据报只构建一次、只递增一次 `message_seq`、只计算一次 CSLP CRC32；两个目的地的 CSLP 应用载荷必须逐字节相同。
5. 两次 `udp_sendto` 必须各自使用独立 pbuf。首个 pbuf 可能仍由 GEM TX 队列持有，不得修改或复用其内存发送第二个目的地。
6. 主链既有计数和错误语义保持不变；镜像使用独立计数，不得污染 `frames_sent`、`packets_sent` 或 `frames_dropped`。
7. 镜像由编译期配置控制，正式比赛构建默认关闭；诊断构建必须在产物身份和日志中明确标记。
8. 本功能不修改 PL、DMA、帧缓冲、滤波器、采样率、分片布局或 P4 接收协议。

## 3. 固定网络拓扑

| 节点 | IPv4/端口 | 角色 |
|---|---|---|
| Zynq FPGA/PS | `192.168.10.2/24:50000` | 唯一 CSLP 服务端和两个 UDP 发送的源 |
| ESP32-P4 | `192.168.10.3/24:50001` | 唯一控制端、正式波形接收端 |
| 调试电脑 | `192.168.10.4/24:50002` | 只接收诊断镜像，不参与 CSLP 控制 |

三者接入同一交换机，不配置 DHCP、网关、DNS、IPv6、组播或 jumbo frame。主发送和镜像发送都使用 IPv4 UDP 单播：

```text
                    ┌── 主发送 ──> ESP32-P4 192.168.10.3:50001
Zynq 192.168.10.2:50000
                    └── 镜像发送 -> 电脑      192.168.10.4:50002
```

电脑的普通交换机端口无法旁听 `.2 → .3` 的单播帧；双发由 Zynq 主动生成发往 `.4` 的第二个 UDP 数据报。两个数据报具有相同 CSLP 应用载荷，但目的 MAC、目的 IP、目的 UDP 端口、IPv4 checksum 和 UDP checksum不同。

## 4. 镜像范围与协议语义

诊断构建镜像所有由 Zynq 发出的合法 CSLP 数据报：

| 消息 | 主目的地 | 是否镜像 | 说明 |
|---|---|---:|---|
| `HELLO_ACK` | P4 | 是 | 电脑可确认服务端接受了哪个 session |
| `CONFIG_ACK` | P4 | 是 | 电脑可核对 Profile 与 `config_id` |
| `ENABLE_PUSH_ACK` | P4 | 是 | 电脑可确认正式推送起点 |
| `DISABLE_PUSH_ACK` | P4 | 是 | 包括帧中途延迟响应 |
| `ERROR` | P4 | 是 | 仅镜像 Zynq 实际发出的错误响应 |
| `STATUS` | P4 | 是 | 镜像原始 STATUS，不扩展 v0.1 载荷 |
| `WAVE_DATA` | P4 | 是 | 12 个分片逐包镜像 |

P4 发往 Zynq 的控制请求不会出现在电脑镜像中。电脑只能观察 FPGA 出站半链路；若需要完整双向原始 pcap，仍需交换机端口镜像、以太网 TAP 或内联网桥。

镜像数据报必须保留主发送的：

- `session_id`、`message_type`、`message_seq` 和 `timestamp_us`；
- `config_id`、`frame_id`、chunk 索引、flags 和全部元数据；
- S16_LE 样点字节；
- CSLP CRC32。

禁止为镜像重新生成 session、序号、时间戳或 CRC。电脑收到的 `message_seq` 缺口只表示镜像路径可能丢包，不能直接宣称 P4 主链发生同样缺口。

## 5. 构建配置

在 `Zynq_7010_PS/cyclescope_cslp/scripts/build_vitis.py` 中增加并校验以下构建变量：

| 环境变量 | 编译定义 | 默认值 | 约束 |
|---|---|---:|---|
| `CSLP_MIRROR_ENABLED` | `CSLP_MIRROR_ENABLED` | `0` | 只能为 `0` 或 `1` |
| `CSLP_MIRROR_IPV4_LAST_OCTET` | `CSLP_MIRROR_IPV4_LAST_OCTET` | `4` | `1..254`；镜像开启时不得等于本机 `.2` 或本次构建的 peer |
| `CSLP_MIRROR_UDP_PORT` | `CSLP_MIRROR_UDP_PORT` | `50002` | `1..65535`，不得等于服务端端口 `50000` 或正式数据端口 `50001` |

生产构建继续使用：

```bash
CSLP_PEER_IPV4_LAST_OCTET=3 \
CSLP_MIRROR_ENABLED=0 \
make vitis
```

双发诊断构建使用：

```bash
CSLP_PEER_IPV4_LAST_OCTET=3 \
CSLP_MIRROR_ENABLED=1 \
CSLP_MIRROR_IPV4_LAST_OCTET=4 \
CSLP_MIRROR_UDP_PORT=50002 \
make vitis
```

不得复用现有 `vitis-lan-test` 的 `peer=.4` 语义实现双发；该目标让电脑成为唯一控制端，与“P4 为主、电脑为镜像”的目标不同。若新增 Makefile 便捷目标，名称固定为 `vitis-mirror`，内部必须保持 `peer=.3`、真实 ADC/测试图样设置由调用者显式选择，并输出全部镜像配置。

构建日志至少输出：

```text
VITIS_PEER_IPV4=192.168.10.3
VITIS_MIRROR=enabled:1 destination:192.168.10.4:50002
```

镜像关闭时 ELF 中不得保留活动镜像发送路径；镜像打开时应有明确启动日志，但不得逐包打印。

## 6. PS 端发送设计

### 6.1 状态与计数

在 Zynq PS 应用状态中增加：

```text
mirror_address
mirror_enabled
mirror_datagrams_attempted
mirror_datagrams_queued
mirror_send_failures
mirror_arp_unresolved
mirror_arp_requests
mirror_arp_request_failures
next_mirror_arp_request_us
```

这些字段只属于本地诊断，不加入 CSLP v0.1 `STATUS` 载荷，以免改变冻结的线上协议。诊断构建每 5 秒最多输出一次汇总；测试工具也可以通过冻结 ELF 符号和 JTAG 只读方式采集终值。

`mirror_datagrams_queued` 的口径固定为镜像 `udp_sendto` 返回 `ERR_OK`，只表示 lwIP/GEM 接受或排队了该数据报，不表示电脑已经物理收到；电脑接收数量必须由 pcap 或被动接收工具独立证明。

### 6.2 发送接口分层

保留一个只负责单目的地发送的底层函数，其职责为：

1. 校验长度为 `1..1472 byte`；
2. `pbuf_alloc(PBUF_TRANSPORT, length, PBUF_RAM)`；
3. `pbuf_take` 复制已经构建好的 CSLP 字节；
4. 对指定 IP/端口调用 `udp_sendto`；
5. 释放调用者持有的 pbuf 引用并返回 lwIP 结果。

在其上增加“主发送后镜像”的包装层：

```text
send_primary_then_mirror(primary_ip, primary_port, bytes, length):
    primary_result = send_one(primary_ip, primary_port, bytes, length)
    if primary_result != OK:
        return PRIMARY_FAILED

    if mirror is disabled:
        return PRIMARY_OK

    if mirror ARP entry is unresolved:
        mirror_arp_unresolved += 1
        if current_time >= next_mirror_arp_request_us:
            mirror_arp_requests += 1
            issue one ARP request without queueing the UDP payload
            record request failure if any
            next_mirror_arp_request_us = current_time + 1 second
        return PRIMARY_OK

    mirror_datagrams_attempted += 1
    mirror_result = send_one(mirror_ip, mirror_port, bytes, length)
    if mirror_result == OK:
        mirror_datagrams_queued += 1
    else:
        mirror_send_failures += 1
    return PRIMARY_OK
```

主发送结果与镜像结果必须使用不同返回域，调用者只能根据主发送结果改变业务状态。

### 6.3 ARP 策略

镜像路径不得在电脑缺席时无限积压等待解析的 UDP pbuf。诊断开始前，电脑应从 `.4` 主动 ping FPGA `.2`，使 FPGA 提前学习电脑的 ARP 项。镜像发送前使用 lwIP ARP 表查询确认 `.4` 已解析：

- 已解析：执行镜像 `udp_sendto`；
- 未解析：跳过该镜像，增加 `mirror_arp_unresolved`，不得把当前 CSLP 数据报交给 ARP 队列；
- 未解析时最多每秒调用一次 `etharp_request` 发送独立 ARP 请求，使电脑稍后上线后能够自动恢复镜像；
- ARP 请求失败只更新独立计数，不逐包打印，也不改变主发送结果。

主目的 P4 的 ARP、会话和发送路径保持现有行为，不受镜像 ARP 状态影响。

### 6.4 各业务路径的调用规则

- 控制响应：先向合法请求源 P4 发送；主发送成功后镜像同一响应。
- 延迟 `DISABLE_PUSH_ACK`：保持现有“活动帧排空后响应”语义；镜像不得延迟 ACK。
- `STATUS`：构建一次、状态序号递增一次，先发 P4、再镜像。
- `WAVE_DATA`：每个 chunk 构建一次、波形序号递增一次，先发 P4、再镜像。
- 主 WAVE 发送失败：保持现有行为，增加正式 `frames_dropped`、终止当前帧并释放帧所有权；不得再镜像该包。
- 镜像 WAVE 发送失败：主帧继续发送后续 chunk，不改变帧所有权或正式计数。
- 新 session、重配置和 DISABLE：镜像不持有帧引用，不需要额外清空数据队列；仅重置本次会话的镜像统计显示锚点，累计计数可以保留到设备重启。

## 7. 资源与时序预算

当前 routed PL 报告为：LUT `34.99%`、寄存器 `33.41%`、BRAM `34.17%`、DSP `31.25%`，WNS/WHS 为 `+1.006/+0.016 ns`。本方案不修改 PL，以上资源不应因双发发生变化。

Profile 1 每帧的线上占用按以太网前导码、FCS 和帧间隙计算：

```text
11 个满包：11 × 1538 byte
1 个末包： 1 × 1122 byte
单目的：   18040 byte/frame × 20 frame/s × 8
          = 2.8864 Mbit/s
双目的：   5.7728 Mbit/s
```

双发只占 100M 链路约 `5.8%`。一个满包上线约 `123 us`，主包与镜像包连续上线约 `246 us`，低于当前 `500 us` WAVE chunk 调度间隔，保留约 `254 us` 裕量。实现不得因为双发缩短现有 500 us 分片间隔。

当前 BSP/lwIP 基线为：

- RAW API，非阻塞 UDP TX；
- `MEM_SIZE=262144`；
- `MEMP_NUM_PBUF=128`；
- `PBUF_POOL_SIZE=128`，`PBUF_POOL_BUFSIZE=1700`；
- GEM TX/RX descriptor 各 64；
- TX/RX checksum offload 开启。

双发增加约 `345 KB/s` 的 CSLP 字节复制和约 240 次/s 的额外 WAVE UDP 发送，不需要第二个帧缓冲、第二个 UDP PCB 或 PL BRAM。不得用这些静态预算替代实板长稳；实际验收仍需检查 pbuf 分配失败、TX descriptor 耗尽、GEM underrun 和帧周期长尾。

## 8. 电脑端接收与分析

电脑先配置 `.4/24`，确认旧的主动 CSLP 工具已经退出，主动 ping FPGA 以建立 ARP，然后只绑定 UDP 50002。若要看到完整握手，完成 ping 和抓包启动后再让 P4 发起新 session：

```bash
ss -lunp | rg ':50000\b|:50001\b|:50002\b'
ping -I 192.168.10.4 -c 2 -W 1 192.168.10.2

sudo tcpdump -ni enp2s0 -s 0 -B 4096 -U \
  -w reports/fpga-mirror.pcap \
  'src host 192.168.10.2 and dst host 192.168.10.4 and udp dst port 50002'
```

电脑镜像接收工具不得向 `.2:50000` 发送任何控制包。解析器应按现有 CSLP v0.1 规则验证：

- UDP 应用载荷不超过 1472 byte，IPv4 不分片；
- UDP checksum 非零且有效；
- CSLP magic/version/长度/CRC32；
- session/config/frame/sequence；
- 每帧 `11×700 + 492` 点的固定 12 分片；
- `sample_rate_hz=4,062,500`、8192 点、S16_LE、单通道、`filter_profile=1`；
- STATUS、WAVE 和控制 ACK 的时间顺序。

镜像 pcap只能证明 FPGA向电脑发出的副本。它不能证明：

- P4 实际收到了对应主包；
- 主包与镜像包具有相同的目的 MAC/IP/UDP checksum；
- P4→FPGA 控制请求内容；
- 主链原始二层 FCS 或 P4 入站硬件时间戳。

端到端结论必须同时使用 P4 receiver/pipeline 计数和 UI/measurement 证据。

## 9. 故障隔离要求

| 故障 | 镜像预期行为 | 主链必须保持 |
|---|---|---|
| 电脑未开机或网线拔出 | ARP 未解析时跳过；已有缓存时仍可能显示 queued | P4 20 fps、会话和测量不变；送达与否只由电脑侧证明 |
| `.4` ARP 未解析 | 不发送数据镜像，最多每秒独立 ARP 一次 | 不把 CSLP 数据报排入 ARP 等待队列 |
| 电脑未监听 50002 | UDP 正常发出，无需响应 | 不重试、不等待 ICMP |
| 镜像 pbuf 分配失败 | `mirror_send_failures` 增长 | 当前主帧继续 |
| 镜像 `udp_sendto` 失败 | 记录并限速报告 | 不增加正式 `frames_dropped` |
| P4 主发送失败 | 不发送该包镜像 | 保持现有整帧 fail-closed |
| P4 发起新 session | 镜像新 ACK 和后续新 session 数据 | 旧会话停止、重新配置 |
| 帧中途 DISABLE | 镜像剩余主帧和延迟 ACK | 与单目的排空语义一致 |
| 电脑误发 CSLP 控制 | FPGA 来源门禁静默拒绝 | P4 唯一 peer 不变 |

禁止镜像失败触发：DMA 停止、capture disable、会话重建、P4 重连、帧所有权延长、分片节拍重置或日志洪水。

## 10. 实现步骤

1. 在 Vitis 构建脚本中加入镜像开关、目标 IP/端口校验及生成日志；默认关闭。
2. 在 `target/main.c` 中加入镜像配置、独立统计、ARP-ready 查询和每秒最多一次的独立 ARP 请求。
3. 将现有单目的发送函数保留为底层 `send_one`，新增主发后镜像包装层。
4. 让即时控制响应、延迟 DISABLE ACK、STATUS 和 WAVE_DATA 统一经过包装层。
5. 保持 WAVE/STATUS/控制响应各自只构建一次，补主机级 fake-send 测试验证调用顺序与故障隔离。
6. 扩展 LAN 工具增加 `--passive-mirror --bind 192.168.10.4:50002` 模式；该模式禁止发送控制报文，只接收并重组已有 session。
7. 增加 pcap 分析门禁，允许镜像目的 `.4:50002`，但继续严格检查分片、checksum 和 CSLP CRC。
8. 先用 PL 测试图样完成单帧、100 帧和故障矩阵，再使用真实 ADC 完成 10,000 帧双发长稳。
9. 双发验收结束后恢复默认镜像关闭的正式构建，并冻结 ELF/bitstream/配置和哈希。

## 11. 测试与验收门禁

### 11.1 主机与构建测试

- 环境变量空值、非法布尔值、非法 IP octet、非法端口和地址冲突必须使构建失败。
- 镜像关闭构建必须与原单目的业务逻辑等价。
- fake-send 验证严格顺序为 `primary → mirror`，且两个发送收到完全相同的 CSLP bytes。
- 验证序号和 CRC只生成一次；不得为镜像多增一次 WAVE/STATUS sequence。
- 注入主发送失败时不得调用镜像；注入镜像失败时主返回仍为成功。
- 生产 `make vitis` 与诊断构建均须完成 Vitis 2025.1 全量构建。

### 11.2 分阶段板级测试

1. **镜像关闭基线**：P4 完成握手、1 帧和100帧，行为与改动前一致。
2. **镜像开启、电脑在线**：电脑先建立 ARP，P4 建立会话；两端同时完成100帧，PC与P4看到的 CSLP payload CRC/sequence/frame完全对应。
3. **电脑离线/稍后上线**：先不接电脑运行 P4 1000帧，再接入 `.4`；离线期间主链不得出现新增 drop、重连或不完整帧，电脑上线并响应 ARP 后镜像应自动恢复。
4. **P4重连**：P4重启并生成新 session；电脑镜像必须观察到新 ACK 和新 session WAVE，旧 session不得继续发送。
5. **DISABLE边界**：在帧中途请求 DISABLE，主链和镜像均只排空允许的活动帧，然后出现同一 ACK。
6. **双发长稳**：真实 ADC 运行至少10,000帧；P4完成全部帧，受控在线窗口内 PC 镜像必须重组全部预期副本且无 sequence/chunk 缺口。电脑侧出现缺口时镜像链路判定不通过或单独降级记录；FPGA queued/ARP/send-failure 计数不能解释交换机、网卡、内核或电脑应用层丢包。

### 11.3 最终 PASS 条件

- P4 主链维持 `20 frame/s`，完整帧、分析和发布计数一致；
- 主链 source/session/config/CRC/分片/duplicate/stale/busy/overrange/FIFO 错误无非预期增长；
- 镜像开启与关闭相比，帧周期 p99/max、首帧时间和2秒显示目标无不可接受退化；
- PC pcap 无 IPv4 分片、零/坏 UDP checksum、坏 CSLP CRC、错误12分片布局或元数据漂移；
- FPGA GEM TX underrun、buffer exhausted、collision/carrier、FCS/length/symbol/alignment和checksum错误均为0；
- 电脑离线、ARP 未解析和镜像发送失败均不影响 P4 主链；
- 最终证据绑定源码状态、Vitis配置、bitstream/ELF哈希、P4镜像身份、UART、pcap和两端JSON报告。

## 12. 回退与正式交付

镜像是诊断能力，不是比赛功能依赖。任何双发测试出现主链退化时，立即使用 `CSLP_MIRROR_ENABLED=0` 重建 PS ELF；PL bitstream和 P4 固件无需因此修改。

正式比赛交付默认关闭镜像，继续保持：

```text
FPGA 192.168.10.2:50000 <-> P4 192.168.10.3:50001
唯一 peer = P4
mirror = disabled
```

若决定在现场保留镜像功能，也必须以“编译存在但默认关闭”的方式交付，不得要求电脑在线、ARP 可用或 UDP 50002 可达才能完成正式测量。

## 13. 当前实现与预验收记录（2026-08-01）

本轮已完成且通过：

- PS端主发后镜像、两次独立pbuf、ARP-ready门、每秒一次ARP请求和独立限速统计；
- 构建变量严格校验，镜像关闭ELF无活动标识、镜像开启ELF有明确身份；
- 电脑端`--passive-mirror`严格零控制写入，支持中途锁定session/config、缓存ACK重放和
  同序号冲突响应；
- 同一pcap内主端与镜像端CSLP载荷逐包、逐字节比较工具；
- C主机测试及Python共81项测试通过，24项仅因当前环境依赖按设计跳过；Vitis 2025.1
  镜像关闭和开启两种构建均通过；
- 只对A9#0执行处理器级复位并下载PS ELF，未重配PL、未写QSPI、未访问MIO47；
- 电脑临时绑定`.3:50001`模拟主端，既有`.4:50002`作为被动镜像端；100帧短测实际
  排空101帧，两端各收到1212个WAVE包，应用、ADC状态和NIC错误均为0；
- 1,000帧专项稳定测试实际排空1,001帧，两端各收到12,012个WAVE包，稳态帧率均约
  20.000 Hz，真实ADC样点范围均为`-85..110`，镜像端`network_writes=0`；
- 稳定测试pcap的两路各有12,121个CSLP数据报，全部UDP checksum有效、无IPv4分片，
  tcpdump内核丢包为0；两路CSLP载荷序列总SHA-256均为
  `6850534f92f49bf3114934d1166735c401acd907178756b4710121e0dbbb38f5`，逐包无差异；
- 两轮测试的临时`.3`均由退出陷阱删除，最终电脑只保留`.4`，50001/50002监听进程和
  `hw_server`均已退出。

以上预验收只证明Zynq双发和电脑模拟主/镜像端闭环，不能替代真实P4接收证据。后续仍
需在P4参与时完成电脑离线/恢复、P4重连、DISABLE边界及双发长稳；正式比赛构建继续
默认`CSLP_MIRROR_ENABLED=0`。
