# ESP32P4-FPGA 联调指南（ESP32-P4 端）

## 1. 文档目的与依据

本文用于 CycleScope 的 ESP32-P4 与 Zynq-7010 FPGA 首轮及后续联合调试，重点说明 ESP32-P4 端的准备、构建、烧录、协议门禁、日志判定和故障定位。

权威协议与项目参数见：

- [CSLP UDP 通信协议 v0.1](../CSLP-UDP-通信协议-v0.1.md)
- [CSLP G题采样与处理 Profile v0.1](../CSLP-G题采样与处理-Profile-v0.1.md)
- [AD9226 通道 A 与 Zynq-7010 接线定义](../AD9226通道A与Zynq-7010接线定义.md)

若本文与上述文档冲突：

1. 线上字节格式、状态机、CRC、分片和错误语义以 CSLP 主协议为准。
2. 采样率、帧长、滤波指标和验收参数以 G 题 Profile 为准。
3. 本文只规定联调操作顺序和 ESP32-P4 端判定方法，不重新定义协议。

## 2. 当前交接状态（2026-07-31）

- ESP32-P4 端接收、重组、三缓冲、FFT、测量、时域/频域投影、LVGL bridge、生命周期回滚和数据新鲜度状态机均已完成电脑合成数据验证。
- 修复前 normal v5 镜像完成过 10,000 帧、题目边界矩阵、高阶谐波矩阵和滤后残余测试；最终 freshness 镜像另完成构建、烧录、Flash 读回及 `ONLINE STALE → OFFLINE STALE → 新有效帧恢复 LIVE`。
- 当前板上固件仍是电脑模拟器联调版本，只接受 `192.168.10.5:50000`；开始真实 FPGA 联调前必须重新构建并烧录接受 `192.168.10.2:50000` 的无夹具镜像。
- 当前网络基础检查中，ESP32-P4 `192.168.10.3` 与 FPGA `192.168.10.2` 均可从调试电脑正常 ping 通。
- CP2102N 串口设备号曾从 `/dev/ttyUSB0` 变化为 `/dev/ttyUSB1`。后续必须优先使用 `/dev/serial/by-id/` 稳定路径，不得把 `ttyUSB0` 写死为设备身份。
- 历史联调阶段的 P4 软件改动曾位于 `CycleScope-main/main@94dab8f-dirty`；`94dab8f` 只是当时的合并基线，不能单独重建当时尚未提交的联调源码。当前构建只认仓库根目录的 `main`，正式取证仍必须同时冻结源码差异、ELF/BIN 哈希和 FPGA 产物身份。

## 3. 固定拓扑与角色

| 节点 | IPv4/端口 | 角色 |
|---|---|---|
| Zynq FPGA/PS | `192.168.10.2/24:50000` | CSLP 服务端、控制响应端、波形发送端 |
| ESP32-P4 | `192.168.10.3/24:50001` | CSLP 控制发起端、波形接收/分析/显示端 |
| 调试电脑 | 建议 `192.168.10.4/24` | 串口、抓包、ping 和日志取证 |
| 电脑模拟器 | 临时 `192.168.10.5/24:50000` | 仅用于 P4 独立测试；真实 FPGA 联调时必须退出 |

关键约束：

- ESP32-P4 主动发起 `HELLO → CONFIG_SET → ENABLE_PUSH`；不能等待 FPGA 主动发送 HELLO。
- P4 只接受固件配置的唯一对端 IP 和源端口 50000。固件仍配置 `.5` 时，来自 `.2` 的合法 FPGA 包也会被来源门禁拒绝。
- FPGA 必须在 P4 启动或复位前监听 `.2:50000`，这样才能取得干净的冷启动握手证据。
- P4、FPGA 和电脑位于同一 `/24` 网段，不配置网关、DNS、DHCP、组播、IPv6 或 jumbo frame。
- 真实 FPGA 联调时，电脑不得绑定 `.5:50000` 运行模拟器，也不得伪装成 `.2`。

## 4. Profile 1 必须完全一致

| 字段/参数 | 固定值 |
|---|---:|
| `sample_rate_hz` | `4,062,500` |
| `frame_sample_count` | `8,192` |
| `frame_period_us` | `50,000`（20 frame/s） |
| `sample_format` | `1`（S16_LE） |
| `channel_count` | `1` |
| `filter_profile` | `1`（`G_BAND_500K_DECIM16`） |
| UDP 应用载荷上限 | `1,472 byte` |
| WAVE_DATA 头 | `72 byte` |
| 每帧分片 | `12` 包 |
| chunk 0～10 | 每包 `700` 点、`1,400 byte` 样点载荷 |
| chunk 11 | `492` 点、`984 byte` 样点载荷 |

注意：

- `sample_rate_hz` 是 65 MSPS 经低通和 16 倍抽取后的线上连续样点率，不是 ADC 原始 65 MHz。
- 单帧采集窗约为 `2.016492 ms`，50 ms 是投递周期；两者不得混写。
- WAVE_DATA 多字节头字段使用网络字节序，样点载荷使用 S16_LE。
- 正式帧必须置 `FILTERED`，携带 CONFIG_ACK 返回的非零 `config_id`，并保持 `filter_profile=1`。
- UDP checksum 必须有效，CSLP CRC32 使用 CRC-32/ISO-HDLC；禁止 IPv4 分片。
- 一帧 8192 点必须来自连续、等间隔、无重复、无缺口的抽取后数据。短 DMA、超长 DMA、TLAST 错位或元数据不同步都应在 FPGA 端放弃整帧。

## 5. 联调前检查

### 5.1 工作树与源码身份

ESP32-P4 端只在仓库根目录的 `main` 分支构建，不从历史 FPGA 或旧 ESP32-P4 worktree 借用未冻结源码。

```bash
git symbolic-ref --short HEAD
git rev-parse HEAD
git status --short --branch
git diff --check
```

预期分支为 `main`。若工作树为 dirty，必须在记录中明确写出 `HEAD-dirty`，并保存最终 ELF/BIN 哈希；不得把 HEAD commit 写成已经包含全部未提交改动。

### 5.2 网络与端口

```bash
ip -brief address show
ip route get 192.168.10.2
ping -I 192.168.10.4 -c 2 -W 1 192.168.10.2
ping -I 192.168.10.4 -c 2 -W 1 192.168.10.3
ip neigh show 192.168.10.2
ip neigh show 192.168.10.3
ss -lunp | rg ':50000\b|:50001\b'
```

联调前应满足：

- FPGA `.2` 和 P4 `.3` 均可达。
- 电脑没有进程占用 UDP 50000/50001。
- 电脑模拟器已经退出。
- 电脑不再保留会与真实 FPGA 角色冲突的 `.2` 地址。
- `.5` 可保留为未使用的本地别名，但更推荐在真实 FPGA 取证时撤掉，避免误启动模拟器。

### 5.3 串口

```bash
find /dev/serial/by-id -maxdepth 1 -type l -printf '%p -> %l\n'
lsusb | rg -i 'cp210|silicon labs|10c4:ea60'
fuser -v /dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_32bfefed68c3ee119887c30f9e1b1c54-if00-port0
```

建议统一设置：

```bash
P4_UART=/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_32bfefed68c3ee119887c30f9e1b1c54-if00-port0
```

注意事项：

- 打开 CP2102N 可能切换 DTR/RTS 并触发 P4 复位；需要冷启动证据时这是预期行为，但不能把它误判为随机重启。
- 串口采集应使用明确时长，日志写入 `/tmp`，结束后确认没有遗留 `picocom`、monitor 或捕获进程。
- 若 `/dev/ttyUSB*` 消失，先查 `/dev/serial/by-id` 和 `lsusb`。只有 CP2102N 在 USB 层也消失且重新插拔/供电无法恢复时，才按不可恢复串口故障停止联调。

## 6. 构建 FPGA `.2` 无夹具固件

### 6.1 配置要求

进入 ESP-IDF 6.0.2 环境后，在 `CycleScope CSLP Configuration` 中确认：

- `CSLP peer IPv4 address = 192.168.10.2`
- `Enable the CSLP frame ownership diagnostic consumer = n`
- `Run one DISABLE/CONFIG/ENABLE lifecycle test = n`
- `CYCLESCOPE_LOCAL_TEST_CMAKE` 为空
- heap tracing 关闭
- 编译优化为性能模式

当前测量链固定调用经目标板验证的 `dsps_fft2r_fc32_ansi`。ESP32-P4 的 ARP4 FFT 内核曾在 10 kHz 弱基波、特定相位输入下产生错误正谱；未经 exact weak 向量的目标板 A/B、题目边界矩阵和性能回归，不得恢复 ARP4，也不得通过放宽“至少两根谐波线”或降低阈值绕过启动门禁。

项目 Kconfig 的生产默认值已经是 `.2`，但本地 `sdkconfig` 可能仍保留 `.5` 覆盖，必须检查生成后的配置，不能只看默认文件。

推荐使用新的构建目录，避免复用 `.5` 产物：

```bash
cd ESP32-P4
source /home/feisibo/.espressif/v6.0.2/esp-idf/export.sh
idf.py menuconfig
idf.py -B /tmp/cyclescope-p4-fpga-build build
```

构建后至少检查：

```bash
rg -n 'CONFIG_CYCLESCOPE_CSLP_PEER_IPV4' \
  /tmp/cyclescope-p4-fpga-build/config/sdkconfig.h
rg -n '^CYCLESCOPE_LOCAL_TEST_CMAKE' \
  /tmp/cyclescope-p4-fpga-build/CMakeCache.txt
sha256sum \
  /tmp/cyclescope-p4-fpga-build/CycleScopeP4.elf \
  /tmp/cyclescope-p4-fpga-build/CycleScopeP4.bin
stat -c '%n size=%s' /tmp/cyclescope-p4-fpga-build/CycleScopeP4.bin
```

硬门禁：生成头中必须是 `192.168.10.2`，本地测试片段必须为空。任何 fault fixture、diagnostic consumer 或 `.5` 字符串进入正式配置时，不得烧录为 FPGA 联调镜像。

### 6.2 烧录

确认串口空闲后：

```bash
idf.py -B /tmp/cyclescope-p4-fpga-build -p "$P4_UART" flash
```

烧录工具必须对 bootloader、partition table 和 app 三段完成写后校验。需要冻结镜像身份时，再从 app 分区 `0x20000` 按 BIN 实际大小完整读回，与 BIN 执行 `cmp` 和 SHA256 对比。

烧录或读回会复位 P4。FPGA 应提前运行并监听 `.2:50000`，否则启动日志中的握手 timeout 只是“对端尚未就绪”，不能作为协议失败证据。

## 7. 串口采集与预期启动顺序

项目现有有界捕获脚本会硬复位目标：

```bash
python3 tool-of-rei/test/capture_p4_serial.py \
  --port "$P4_UART" \
  --baud 115200 \
  --seconds 45 \
  --output /tmp/cyclescope-p4-fpga-smoke-uart.log
```

正常启动的关键顺序应为：

1. ESP32-P4、Flash、PSRAM 和双核启动正常。
2. Display/LVGL、普通 FFT 自检和 exact weak FFT 自检 PASS。
3. IP101 Link Up，P4 发布静态 IPv4 `192.168.10.3`。
4. receiver 日志明确为 `UDP 192.168.10.3:50001 -> 192.168.10.2:50000`。
5. FPGA 返回 HELLO_ACK、CONFIG_ACK、ENABLE_PUSH_ACK。
6. P4 打印 `CSLP session ready`。
7. 首个完整合法帧触发 `Published frame=1`、`measurement` 和 UI bridge。
8. UI 从 WAITING 或 STALE 转为 LIVE。

约 3.5～4.4 秒的 PHY/系统冷启动只记录，不属于专家操作后的 2 秒显示时限。联调时不得通过缩短 PHY reset timing、关闭自检或删除安全门禁来“优化”这个数字。

## 8. 分阶段联调门禁

### 8.1 阶段 A：纯网络与握手

目标：证明两端地址、端口、字节序、CRC、能力位和控制状态机一致。

PASS 条件：

- P4 日志目标为 `.2:50000`。
- HELLO_ACK 回显 P4 生成的非零 session 和 message sequence。
- 能力位 0～4 完整，`max_udp_payload=1472`。
- CONFIG_ACK 返回非零 `config_id`，实际配置逐项等于 Profile 1。
- ENABLE_PUSH_ACK 为 OK，随后出现唯一对应的 `CSLP session ready`。
- 控制重试使用同键同载荷时，FPGA 返回缓存响应，不重复执行副作用。

禁止行为：

- CONFIG 失败后仍发送波形。
- HELLO 源端口与 data_port 不同。
- FPGA 沿用旧 session/config。
- ACK 的 session、message sequence 或载荷长度与请求不匹配。

### 8.2 阶段 B：单帧与 100 帧烟测

先发送 1 帧，再连续发送 100 帧。不要一上来直接跑 10,000 帧，否则错误只会被日志洪水掩盖。

单帧 PASS 条件：

- 共 12 个 WAVE_DATA，前 11 包 700 点，末包 492 点。
- 所有包使用同一 session、frame、config、flags、比例、偏置、calibration 和时间戳元数据。
- P4 只在 12 个分片完整且 CRC/元数据均合法后发布完整帧。
- 出现 `measurement`；F0、Vpp、RMS 和谱线结果有限且合理。
- session-ready 本身不能让旧数据显示 LIVE；必须由新有效 measurement 恢复 LIVE。

100 帧 PASS 条件：

- `completed`、`acquired`、`analyzed`、`published` 均覆盖正式 100 帧窗口。
- source/magic/version/length/session/CRC、incomplete、duplicate、stale、busy、config、metadata、overrange、FIFO、socket fatal、FFT failure 均无非预期增长。
- 无 WDT、panic、assert、abort、复位或不可恢复内存下降。
- 数据停止后 P4 进入 STALE；重新建立新 session 时，只有新有效帧可恢复 LIVE。

### 8.3 阶段 C：受控故障

在 FPGA 端逐项注入，不要同时混合：

- 丢一个分片：整帧不得发布，`incomplete` 精确增长。
- 重复相同分片：不得重复计入样点；冲突重复必须拒绝。
- CRC 错误：数据报丢弃，坏帧不得进入 FFT。
- 错 session/config：静默拒绝，不污染当前重组。
- 元数据冲突：整帧作废。
- `ADC_OVERRANGE` 或 `FIFO_OVERFLOW`：帧不得进入正常测量。
- ENABLE_ACK 前发送 WAVE：不得进入重组或计数为有效帧。
- DISABLE/重配置：ACK 后不得再排入旧 config 波形。
- FPGA 重启或 peer-silent：P4 必须生成新 session，清空旧 assembling/latest，旧结果不得闪回。

每个故障后都必须用一个新合法 session 或后续合法帧证明可恢复，不能只证明“会拒绝”。

### 8.4 阶段 D：题目边界与长期运行

协议烟测通过后再进行：

1. 已知数字 test pattern，核对 S16_LE、比例、偏置、F0/Vpp/RMS 和谱线。
2. G 题四边界与高阶弱分量矩阵。
3. 真实 ADC 通道 A、码型、极性、OTR 和校准。
4. 1 MHz 以上干扰与数字滤波阻带测试。
5. 直连和交换机路径各 10,000 帧。
6. 两板冷启动、热复位、FPGA 重启和断线恢复。

P4 电脑合成数据的数值 PASS 不能替代 FPGA FIR、DMA、ADC、模拟前端或真实干扰结果；长期测试必须绑定本次 FPGA bitstream/软件、P4 ELF/BIN 和原始日志。

## 9. ESP32-P4 严格接收门禁

以下任何条件不满足时，P4 都不得发布测量：

- 源 IP 不是固件配置的 `.2`，或源端口不是 50000。
- magic、协议版本、header/packet/payload 长度不合法。
- CSLP CRC32 或 UDP checksum 错误。
- session 未 ready，或 session 不匹配。
- WAVE 的 config_id、sample rate、frame count、period、format、channel、filter profile 不匹配。
- 分片索引、offset、样点数不符合固定公式。
- 同一帧共享元数据冲突。
- 帧不完整、过旧、被新帧替代，或分析结束时已不再 current。
- `ADC_OVERRANGE`、`FIFO_OVERFLOW`、FFT 或投影 fail-closed。

STATUS 只能证明 transport/session 仍在线，不能证明屏幕数据仍新鲜。P4 以最后成功应用到 UI 的 current 有效帧为锚；1000 ms 无新有效帧后显示 `ONLINE • STALE`，peer-silent 后显示 `OFFLINE • STALE`，只有新有效 measurement 恢复 LIVE。

## 10. 抓包与证据保存

建议同时保存：

- P4 UART 原始日志。
- FPGA/PS 控制与发送日志。
- 电脑抓取的 pcap。
- P4 ELF/BIN/Flash 读回 SHA256。
- FPGA bitstream、PS 可执行文件及其源码 commit/hash。
- 接线、信号源、校准、交换机/直连方式和测试时间。

抓包重点：

- `.3:50001 → .2:50000` 控制请求。
- `.2:50000 → .3:50001` ACK、STATUS 和 WAVE。
- UDP checksum 有效，无 IPv4 fragmentation。
- 20 frame/s，单帧 12 包，message sequence 与 frame_id 单调。
- STATUS 计数和 P4 receiver health 可相互解释。

测试源码和解析夹具应放在 `tool-of-rei/test/`；`/tmp` 只保存构建产物、UART、sender、pcap 和 Flash 读回。sender 打印“发送完成”不能单独证明 P4 已完成接收/分析/显示，必须由 UART session/frame/measurement/health 闭环。

## 11. 常见故障定位

| 现象 | 首要检查 | 处理原则 |
|---|---|---|
| `.2/.3` 均 ping 通但无 HELLO_ACK | P4 固件是否仍配置 `.5`；FPGA 是否绑定 `.2:50000` | 先修地址/端口，不改协议超时掩盖错误 |
| P4 不断打印 handshake timeout | FPGA 是否在 P4 复位前监听；ACK CRC/session/seq/长度是否正确 | 抓包看请求是否到达、ACK 是否返回 |
| HELLO 成功但 CONFIG 失败 | 六个 Profile 字段、能力位、字节序 | FPGA 必须回报实际配置；不允许 P4 硬解默认值 |
| session ready 但 completed 不增长 | ENABLE 前发 WAVE、错误 session/config、CRC、来源端口或分片公式 | 对照第一帧 12 包逐字段检查 |
| `incomplete` 增长 | 缺包、DMA 短帧、交换机微突发、发送队列调度 | 先确认严格 12 包和 pcap，再调整包间调度；不能靠扩大重组超时掩盖持续丢包 |
| `metadata` 或 `config` reject | 同帧元数据不原子、config_id 沿用/变化 | FPGA 在冻结一帧时同时冻结完整元数据快照 |
| measurement invalid/FFT failure | 样点字节序、码型、比例、偏置、全零/非有限数据、缺少合法谐波族 | 先用已知数字 test pattern 隔离 ADC/滤波问题 |
| transport ONLINE 但 UI STALE | STATUS 仍到达，但超过 1000 ms 没有新有效帧 | 查 completed→acquired→analyzed→published 哪一级停止，不得把旧数据显示为 LIVE |
| 串口 `/dev/ttyUSB0` 不存在 | `/dev/serial/by-id`、`lsusb`、是否变为 ttyUSB1 | 使用 by-id；USB 层也消失且无法恢复时停止 |
| 首次打开串口后板卡重启 | CP2102N DTR/RTS | 有界捕获会硬复位，按冷启动证据处理；避免多个监视器抢占 |
| FFT 偶发超过 50 ms | 同时检查分析/发布计数和 failure/stale | 单次调度长尾不等于业务失败；若造成计数脱节再做性能诊断 |

## 12. 联调完成判据

ESP32-P4 与 FPGA 的首轮数字链联调只有同时满足以下条件才可标记 PASS：

- 板上明确运行 `.2` 无夹具 P4 镜像，ELF/BIN/Flash 身份闭环。
- FPGA `.2` 与 P4 `.3` 完成标准握手，Profile 1 逐字段一致。
- 单帧、100 帧、故障拒绝与恢复均有 P4 UART 和 FPGA/pcap 配对证据。
- 正式窗口内接收、重组、配置、socket、FFT、投影、WDT 和内存门禁通过。
- 10,000 帧长期运行绑定双方最终产物，不拼接电脑模拟器或旧镜像证据。
- 断线、FPGA 重启和新 session 恢复后旧数据不会闪回 LIVE。

这仍只代表“FPGA → ESP32-P4 数字链 PASS”。真实 BNC、模拟前端、AD9226 码型/极性/OTR、数字滤波指标、200 mVpp 干扰、单路 5 V、屏幕尺寸和按键到真实 panel flush 仍需按 G 题要求独立验收。
