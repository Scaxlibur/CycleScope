# CSLP UDP 通信协议 v0.1

## 1. 文档状态与适用范围

- 协议名称：CycleScope LAN Protocol
- 缩写：CSLP
- 线上版本：1
- 文档状态：v0.1 已在 `fpga-v1.0.0@038e981` 与仓库根 `main` 的 ESP32-P4 主链落地；本文同时保留互操作规范和复现测试步骤
- 最近校对：2026-08-02；真实 `.2 → .3` 握手、连续 600 帧与 M12 联合验收的范围见 [测试与证据索引](../测试与证据索引.md)
- 数据方向：波形由 Zynq 单向推送至 ESP32-P4，控制命令由 ESP32-P4 发起
- 消费策略：latest-frame-wins；尚未被业务层取得的旧完整帧允许被新完整帧覆盖

本文定义线上字节格式、分片规则、控制握手、接收缓冲和异常处理。当前比赛使用的 ADC、抽取倍数、帧长、滤波器及两端职责见 [CSLP G题采样与处理 Profile v0.1](./CSLP-G题采样与处理-Profile-v0.1.md)。

本协议中的 `sample_rate_hz` 一律表示**滤波、抽取后，实际写入 WAVE_DATA 载荷的连续样点率**。原始 ADC 时钟和抽取前采样率不在线上传输，也不得填入该字段。当前 Profile 中该值为 `4,062,500`，不是 `65,000,000`。

文中的“必须”“不得”是互操作要求；“建议”是实现建议。

## 2. 设计目标与非目标

设计目标：

1. ADC 采集连续运行，波形按协商后的 `frame_period_us` 独立投递，不受 P4 分析和显示节奏阻塞。
2. P4 只向业务层发布完整、合法、校验通过且配置身份匹配的波形帧。
3. 丢包、乱序、重复包、旧帧和旧配置帧不能污染已发布数据。
4. 两端只保留有限数量的帧缓冲，不建立无限队列。
5. 所有 UDP 数据报适配标准 1500 字节 IPv4 MTU，不依赖 IP 分片或巨帧。
6. 所有字段均显式序列化，不依赖 C/C++ 结构体布局、对齐或主机字节序。

非目标：

- 不保证每一帧必达。
- 不为 WAVE_DATA 提供 ACK、重传或背压。
- 不提供拥塞控制、加密、身份认证或防重放保护。
- 不在协议层描述原始 ADC 总线、FIR 系数、FFT 窗函数或 UI 格式。

## 3. 系统角色与默认网络参数

| 项目 | Zynq | ESP32-P4 |
|---|---|---|
| 角色 | 采集服务端、控制响应端、波形发送端 | 控制端、波形接收与分析端 |
| 默认 IPv4 | 192.168.10.2/24 | 192.168.10.3/24 |
| 默认 UDP 端口 | 50000，接收控制命令并作为发送源端口 | 50001，发送控制命令并接收响应、状态和波形 |
| 网关/DNS | 不配置 | 不配置 |
| MTU | 1500 | 1500 |

P4 使用同一个绑定到本地端口 50001 的 socket，向 Zynq 端口 50000 发送控制命令。Zynq 从端口 50000 将控制响应、状态和波形发送到 HELLO 指定的 P4 地址和端口。地址与端口应可配置，上述值只是首轮联调默认值。

首版不使用 DHCP、广播发现、IPv6、组播或 jumbo frame。经交换机抓包时，可为调试电脑配置同网段静态地址。

## 4. UDP 使用约束

1. 使用 IPv4 UDP 单播。
2. v0.1 的 UDP 应用载荷上限固定为 1472 字节：1500 字节 IPv4 MTU减去 20 字节 IPv4 头和 8 字节 UDP 头。
3. `HELLO.max_udp_payload` 在 v0.1 中必须等于 1472；其他值返回 `UNSUPPORTED`。该字段为后续版本协商较小 MTU 预留，v0.1 不进行动态分片尺寸协商。
4. 发送端必须禁止 IP 分片，不得生成超过 1472 字节的 UDP 应用载荷。
5. IPv4 UDP checksum 必须启用，不允许发送 checksum 为 0 的数据报；硬件 checksum offload 可以使用。
6. CSLP 仍保留逐数据报 CRC32，用于检测 DMA、缓存、序列化和应用内存错误。
7. P4 必须持续调用 `recvfrom` 排空 socket；latest-frame-wins 发生在完整帧发布后，不能用“暂时不读 socket”代替。
8. 两端必须校验源 IP、源端口、`session_id`、`magic`、`version` 和长度，拒绝其他来源的数据。

## 5. 基础类型与字节序

| 类型 | 含义 |
|---|---|
| u8 | 8 位无符号整数 |
| u16 | 16 位无符号整数 |
| u32 | 32 位无符号整数 |
| u64 | 64 位无符号整数 |
| i32 | 32 位有符号二进制补码整数 |

规则：

- 所有 CSLP 头字段和控制载荷中的多字节整数采用网络字节序，即大端。
- 波形样点 v0.1 采用 S16_LE，即 16 位有符号二进制补码、小端。
- 不在线上传输 `float` 或 `double`。
- 不允许把 C/C++ 结构体直接强制转换为字节数组发送；两端必须按字段偏移显式读写。
- 接收端进行电压换算时必须使用至少 64 位有符号中间值。

## 6. 公共头

所有 CSLP 消息均以 32 字节公共头开始。

| 偏移 | 长度 | 字段 | 类型 | 说明 |
|---:|---:|---|---|---|
| 0 | 4 | magic | byte[4] | 固定为 ASCII `CSLP`，线上字节为 `43 53 4C 50` |
| 4 | 1 | version | u8 | 固定为 1 |
| 5 | 1 | message_type | u8 | 消息类型 |
| 6 | 2 | header_bytes | u16 | 普通消息为 32，WAVE_DATA 为 72 |
| 8 | 4 | session_id | u32 | 非零会话号 |
| 12 | 4 | message_seq | u32 | 消息序号或控制事务号 |
| 16 | 8 | timestamp_us | u64 | 设备启动后的单调微秒时间 |
| 24 | 2 | payload_bytes | u16 | `header_bytes` 之后的载荷长度 |
| 26 | 2 | flags | u16 | 消息标志；未定义位必须为 0 |
| 28 | 4 | crc32 | u32 | CSLP 应用层 CRC32 |

必须满足：

- UDP 应用载荷长度必须等于 `header_bytes + payload_bytes`，不允许尾随字节。
- `header_bytes` 至少为 32，且不得大于实际 UDP 应用载荷。
- 已知消息类型的 `header_bytes` 必须等于该类型在 v0.1 中规定的值。
- 接收端遇到未知版本、非法长度或保留位非零时必须丢弃消息。
- `session_id` 由 P4 在 HELLO 中生成。Zynq 接受后，在该会话所有响应、状态和波形中原样携带。
- HELLO 是会话校验的唯一例外：Zynq 可以接受新的非零 `session_id` 并用它替换旧会话；其他消息会话不匹配时必须静默丢弃。

`timestamp_us` 语义：

- 控制、响应、状态和错误消息：发送端当前单调时间。
- WAVE_DATA：首个传输样点所对应的等效 ADC 采集时刻。线性相位 FIR 的固定群延迟应由发送端补偿；不足 1 微秒的部分四舍五入。帧内其他样点的位置由 `sample_rate_hz` 和样点序号推导，不能靠给微秒时间戳逐点累加。
- 同一 `frame_id` 的所有分片必须携带完全相同的 `timestamp_us`。

`timestamp_us` 不是 UTC，也不用于跨设备绝对授时。

## 7. CRC32

CSLP 使用 CRC-32/ISO-HDLC：

| 参数 | 值 |
|---|---|
| 多项式 | 0x04C11DB7 |
| reflected | true |
| init | 0xFFFFFFFF |
| xorout | 0xFFFFFFFF |
| 检查字符串 `123456789` | 0xCBF43926 |

计算规则：

1. 将公共头中的 `crc32` 字段临时置为 0。
2. 按线上实际字节顺序，对 `header_bytes + payload_bytes` 的全部字节计算 CRC。
3. 将结果按网络字节序写入 `crc32` 字段。
4. 接收端采用相同步骤复算并比较。

每个 UDP 数据报独立校验。任意分片 CRC 失败时，该分片不得写入重组缓冲；对应帧最终不能发布。

## 8. 消息类型、序号与控制幂等性

| 值 | 名称 | 方向 | 是否需要响应 |
|---:|---|---|---|
| 0x01 | HELLO | P4 → Zynq | 是，0x81 |
| 0x02 | CONFIG_SET | P4 → Zynq | 是，0x82 |
| 0x03 | ENABLE_PUSH | P4 → Zynq | 是，0x83 |
| 0x04 | DISABLE_PUSH | P4 → Zynq | 是，0x84 |
| 0x10 | STATUS | Zynq → P4 | 否 |
| 0x20 | WAVE_DATA | Zynq → P4 | 否 |
| 0x7F | ERROR | 双向 | 否 |
| 0x81 | HELLO_ACK | Zynq → P4 | 否 |
| 0x82 | CONFIG_ACK | Zynq → P4 | 否 |
| 0x83 | ENABLE_PUSH_ACK | Zynq → P4 | 否 |
| 0x84 | DISABLE_PUSH_ACK | Zynq → P4 | 否 |

序号规则：

- 控制响应的 `message_seq` 必须等于对应请求的 `message_seq`。
- P4 在 100 ms 内未收到合法响应时，使用相同 `message_seq` 和完全相同的载荷重试，最多重试 3 次。
- WAVE_DATA 的 `message_seq` 由 Zynq 对每个 WAVE_DATA 数据报递增。
- STATUS 使用独立、逐消息递增的序号。
- 由合法请求触发的 ERROR，其公共头 `message_seq` 等于请求序号；载荷中的 `offending_seq` 也填该值。非请求触发的 ERROR 使用发送端独立递增序号，`offending_type` 和 `offending_seq` 均为 0。
- 各类递增序号均允许 u32 自然回绕。

Zynq 必须为控制事务实现幂等响应缓存：

1. 缓存键为 `session_id + message_type + message_seq`，同时保存完整请求载荷和完整响应。
2. 再次收到同键、同载荷的请求时，不重复执行副作用，直接返回首次缓存的响应；因此重复 CONFIG_SET 不得再次增加 `config_id`。
3. 收到同键、不同载荷的请求时，不执行请求，以对应类型的 ACK 返回 `SEQ_CONFLICT`；ACK 中除状态码外的结果字段按该消息的失败规则清零。
4. 至少缓存最近 16 个事务，且每项保留时间不得短于 2 秒。
5. 接受新的会话后可清除旧会话缓存；同一会话内不得在保留期内提前复用控制序号。

## 9. 通用状态码

| 值 | 名称 | 含义 |
|---:|---|---|
| 0 | OK | 成功 |
| 1 | BAD_VERSION | 不支持的协议版本 |
| 2 | BAD_LENGTH | 长度非法 |
| 3 | BAD_CONFIG | 采样配置非法 |
| 4 | UNSUPPORTED | 不支持的功能或格式 |
| 5 | BAD_STATE | 当前状态不允许该操作 |
| 6 | BUSY | 设备忙 |
| 7 | INTERNAL_ERROR | 内部错误 |
| 8 | SEQ_CONFLICT | 相同控制事务键出现不同载荷 |

## 10. 控制消息载荷

### 10.1 HELLO

HELLO 请求载荷固定为 8 字节：

| 偏移 | 长度 | 字段 | 类型 | 说明 |
|---:|---:|---|---|---|
| 0 | 2 | data_port | u16 | P4 接收响应和波形的 UDP 端口，默认 50001；不得为 0 |
| 2 | 2 | max_udp_payload | u16 | v0.1 必须等于 1472 |
| 4 | 4 | receiver_caps | u32 | P4 能力位 |

HELLO_ACK 载荷固定为 16 字节：

| 偏移 | 长度 | 字段 | 类型 | 说明 |
|---:|---:|---|---|---|
| 0 | 2 | status | u16 | 通用状态码 |
| 2 | 1 | negotiated_version | u8 | 协商后的版本；v0.1 为 1 |
| 3 | 1 | reserved | u8 | 必须为 0 |
| 4 | 4 | sender_caps | u32 | Zynq 能力位 |
| 8 | 4 | max_frame_samples | u32 | 支持的单通道最大帧样点数 |
| 12 | 4 | device_boot_id | u32 | Zynq 每次启动生成的新非零值 |

`receiver_caps` 和 `sender_caps` 的 v0.1 位定义：

| 位 | 名称 | 含义 |
|---:|---|---|
| 0 | LATEST_FRAME | 支持 latest-frame-wins |
| 1 | S16_LE | 支持 S16_LE 样点 |
| 2 | APP_CRC32 | 支持 CSLP CRC32 |
| 3 | FILTERED_DATA | 支持发送已滤波、抽取的样点 |
| 4 | CONFIG_ID | 支持配置身份校验 |
| 5～31 | reserved | 必须为 0 |

v0.1 双方必须同时声明位 0～4。能力不完整时 HELLO_ACK 返回 `UNSUPPORTED`，不得进入可推送状态。

v0.1 要求 HELLO 的 UDP 源端口与 `data_port` 相同。Zynq 将 HELLO_ACK 发回请求源 IP/端口；端口不一致时返回 `BAD_CONFIG`，避免控制响应与波形被拆到两个未测试的 socket。失败的 HELLO_ACK 仍回显请求的 `session_id` 和 `message_seq`，但载荷中除 `status` 外的字节全部为 0，且不得替换当前有效会话。

成功接受一个新的 HELLO 时，Zynq 必须停止为旧会话排入新的 WAVE_DATA，清除旧会话的未发送队列和配置状态，并进入 IDLE。网络中已经在途的旧会话数据由 P4 根据 `session_id` 丢弃。每个新会话都必须重新 CONFIG_SET，不能沿用上一次会话的隐式配置。

### 10.2 CONFIG_SET

CONFIG_SET 请求载荷固定为 20 字节：

| 偏移 | 长度 | 字段 | 类型 | 说明 |
|---:|---:|---|---|---|
| 0 | 4 | sample_rate_hz | u32 | 期望的**抽取后线上样点率**；0 表示 Zynq 默认值 |
| 4 | 4 | frame_sample_count | u32 | 每通道每帧样点数；0 表示默认值 |
| 8 | 4 | frame_period_us | u32 | 两个投递周期起点的间隔；0 表示默认值 |
| 12 | 1 | sample_format | u8 | v0.1 中 1 表示 S16_LE |
| 13 | 1 | channel_count | u8 | v0.1 必须为 1 |
| 14 | 2 | filter_profile | u16 | 预定义滤波配置；0 表示默认值 |
| 16 | 4 | reserved | u32 | 必须为 0 |

`frame_period_us` 控制快照投递频率，不改变样点时间间隔，也不等于采集窗长度。采集窗长度为 `frame_sample_count / sample_rate_hz`。Zynq 可以连续采集并在每个投递周期冻结最新的连续窗口；不得通过重复旧数据凑足周期。

CONFIG_ACK 载荷固定为 28 字节，返回实际采用的配置：

| 偏移 | 长度 | 字段 | 类型 | 说明 |
|---:|---:|---|---|---|
| 0 | 2 | status | u16 | 通用状态码 |
| 2 | 2 | reserved0 | u16 | 必须为 0 |
| 4 | 4 | config_id | u32 | 成功应用后生成的非零配置身份 |
| 8 | 4 | sample_rate_hz | u32 | 实际抽取后线上样点率 |
| 12 | 4 | frame_sample_count | u32 | 实际每帧样点数 |
| 16 | 4 | frame_period_us | u32 | 实际投递周期 |
| 20 | 1 | sample_format | u8 | 实际样点格式 |
| 21 | 1 | channel_count | u8 | 实际通道数 |
| 22 | 2 | filter_profile | u16 | 实际滤波配置 |
| 24 | 4 | max_frame_samples | u32 | 当前实现允许的最大帧样点数 |

规则：

- CONFIG_SET 只允许在推送关闭时执行。
- 每个首次成功执行的 CONFIG_SET 事务生成新的非零 `config_id`；自然回绕时跳过 0。缓存重放的事务返回原 `config_id`。
- `status != OK` 时，CONFIG_ACK 偏移 4～27 必须全部为 0，现有配置不得改变。
- P4 必须以 ACK 返回的实际配置初始化重组器与分析器，不能假定请求值一定被原样采用。
- 未成功完成 CONFIG_SET 时，ENABLE_PUSH 必须返回 `BAD_STATE`。

### 10.3 ENABLE_PUSH 与 DISABLE_PUSH

ENABLE_PUSH 和 DISABLE_PUSH 请求载荷长度均为 0；对应 ACK 载荷固定为 4 字节：

| 偏移 | 长度 | 字段 | 类型 | 说明 |
|---:|---:|---|---|---|
| 0 | 2 | status | u16 | 通用状态码 |
| 2 | 2 | reserved | u16 | 必须为 0 |

改变配置时，P4 必须依次执行 `DISABLE_PUSH → CONFIG_SET → ENABLE_PUSH`。

收到 DISABLE_PUSH 后，Zynq 不得再开始冻结新帧。若当前帧尚未向 UDP 层排入首个分片，可以整体放弃；一旦首个分片已经排入，就必须把该帧全部分片排完。`DISABLE_PUSH_ACK(OK)` 只能在当前帧处理结束且旧配置 WAVE_DATA 发送队列排空后发出。ACK 发出后不得再向 UDP 层排入旧配置波形。网络中已经在途、晚于 ACK 到达的旧包仍可能存在，P4 必须依靠 `config_id` 和状态机丢弃。

### 10.4 STATUS

STATUS 载荷固定为 40 字节：

| 偏移 | 长度 | 字段 | 类型 | 说明 |
|---:|---:|---|---|---|
| 0 | 2 | device_state | u16 | 0=IDLE，1=READY，2=PUSH_ENABLED，3=FAULT |
| 2 | 2 | last_error | u16 | 最近一次错误码 |
| 4 | 4 | active_config_id | u32 | 当前配置身份；无有效配置时为 0 |
| 8 | 4 | last_frame_id | u32 | 最近开始发送的 `frame_id`；尚未发送时为 0 |
| 12 | 4 | frames_sent | u32 | 已完整排入 UDP 层的帧计数 |
| 16 | 4 | packets_sent | u32 | 已排入 UDP 层的 WAVE_DATA 数据报计数 |
| 20 | 4 | adc_overrange_frames | u32 | 采集期间观察到 ADC OTR 的帧计数 |
| 24 | 4 | fifo_overflow_frames | u32 | 发生 FIFO/DMA 溢出的帧计数 |
| 28 | 4 | frames_dropped | u32 | 因缓冲、DMA 或发送忙而未能形成完整推送帧的计数 |
| 32 | 4 | uptime_ms | u32 | Zynq 运行时间 |
| 36 | 4 | reserved | u32 | 必须为 0 |

Zynq 在有效会话中建议每 500 ms 发送一次 STATUS。STATUS 只用于健康监测，不确认波形帧。计数器允许 u32 自然回绕。

### 10.5 ERROR

ERROR 载荷固定为 12 字节：

| 偏移 | 长度 | 字段 | 类型 | 说明 |
|---:|---:|---|---|---|
| 0 | 2 | error_code | u16 | 通用或设备错误码 |
| 2 | 1 | offending_type | u8 | 引发错误的消息类型；无对应请求时为 0 |
| 3 | 1 | reserved | u8 | 必须为 0 |
| 4 | 4 | offending_seq | u32 | 引发错误的消息序号；无对应请求时为 0 |
| 8 | 4 | detail | u32 | 与错误类型相关的补充值；无定义时为 0 |

对 CRC 错误、未知会话或随机网络数据必须静默丢弃，避免错误报文风暴。对长度足以安全解析、会话有效的控制请求，可用对应 ACK 状态码或 ERROR 报告错误。

## 11. WAVE_DATA 报文

WAVE_DATA 使用 72 字节头：32 字节公共头加 40 字节波形扩展头。

| 偏移 | 长度 | 字段 | 类型 | 说明 |
|---:|---:|---|---|---|
| 32 | 4 | frame_id | u32 | 每个被投递帧递增一次；0 不使用 |
| 36 | 2 | chunk_index | u16 | 当前分片编号，从 0 开始 |
| 38 | 2 | chunk_count | u16 | 当前帧总分片数 |
| 40 | 4 | sample_offset | u32 | 当前分片第一个时间样点在帧内的偏移 |
| 44 | 2 | samples_in_chunk | u16 | 当前分片每通道样点数 |
| 46 | 1 | sample_format | u8 | v0.1 中 1=S16_LE |
| 47 | 1 | channel_count | u8 | v0.1 必须为 1 |
| 48 | 4 | sample_rate_hz | u32 | 当前载荷的实际抽取后样点率 |
| 52 | 4 | frame_sample_count | u32 | 每通道整帧样点数 |
| 56 | 4 | scale_uV_per_lsb | u32 | 一个传输码值对应的输入端微伏数；不得为 0 |
| 60 | 4 | offset_uV | i32 | 输入端电压偏置，单位微伏 |
| 64 | 4 | config_id | u32 | 生成该帧的配置身份；必须匹配 CONFIG_ACK |
| 68 | 2 | filter_profile | u16 | 生成该帧所用滤波配置 |
| 70 | 2 | calibration_id | u16 | 校准版本；0 表示无有效校准 |

电压换算：

```text
u_uV = sample_code × scale_uV_per_lsb + offset_uV
```

`scale_uV_per_lsb` 和 `offset_uV` 必须是折算到装置 BNC 输入端的参数，包含模拟前端增益/衰减与 ADC 码值关系。若尚未完成校准，可以发送名义值，但必须清除 `CALIBRATED` 并令 `calibration_id = 0`。

公共头 `flags` 在 WAVE_DATA 中定义为：

| 位掩码 | 名称 | 含义 |
|---:|---|---|
| 0x0001 | FIRST_CHUNK | `chunk_index == 0` |
| 0x0002 | LAST_CHUNK | `chunk_index + 1 == chunk_count` |
| 0x0004 | FILTERED | 已应用 `filter_profile` 指定的滤波与抽取 |
| 0x0008 | CALIBRATED | 比例和偏置来自 `calibration_id` 指定的有效校准 |
| 0x0010 | ADC_OVERRANGE | 本帧原始采集期间 ADC OTR 有效 |
| 0x0020 | FIFO_OVERFLOW | 本帧采集、滤波或 DMA 期间发生 FIFO/缓冲溢出 |
| 0x0040 | TEST_PATTERN | 载荷为联调测试图样 |
| 其余 | reserved | 必须为 0 |

除 FIRST_CHUNK 和 LAST_CHUNK 外，同一帧所有分片的 flags 必须完全一致。不得用 `ADC_OVERFLOW` 指代 ADC 模拟输入超量程；模拟超量程和数字 FIFO/DMA 溢出是两类故障。

若 `ADC_OVERRANGE` 或 `FIFO_OVERFLOW` 置位，P4 可以保留该帧用于诊断，但不得将其作为有效测量结果发布给 UI。

## 12. 样点载荷与分片

v0.1 只定义单通道 S16_LE：

- 每个样点 2 字节。
- UDP 应用载荷固定上限为 1472 字节。
- WAVE_DATA 头为 72 字节。
- 单个满数据报携带 `(1472 - 72) / 2 = 700` 个样点。

必须满足：

- `payload_bytes = samples_in_chunk × channel_count × bytes_per_sample`。
- `frame_sample_count` 必须为 1～45,874,500，保证 `chunk_count` 可由非零 u16 表示；实际实现还必须受 HELLO_ACK 的 `max_frame_samples` 限制。
- `chunk_count = ceil(frame_sample_count / 700)`，且不得为 0。
- 非末分片必须恰好携带 700 点，`sample_offset = chunk_index × 700`。
- 末分片携带剩余的 1～700 点。
- `sample_offset + samples_in_chunk` 不得超过 `frame_sample_count`。
- 同一 `frame_id` 的 `timestamp_us`、`chunk_count`、`sample_rate_hz`、`frame_sample_count`、`sample_format`、`channel_count`、`scale_uV_per_lsb`、`offset_uV`、`config_id`、`filter_profile` 和 `calibration_id` 必须一致。
- 同一帧除 FIRST_CHUNK/LAST_CHUNK 外的 flags 必须一致。
- FIRST_CHUNK 和 LAST_CHUNK 必须分别与 `chunk_index == 0`、`chunk_index + 1 == chunk_count` 严格一致。
- `frame_id` 和 `config_id` 必须非零；`CALIBRATED` 置位当且仅当 `calibration_id != 0`。
- 所有分片最终必须精确覆盖样点区间 `[0, frame_sample_count)`，无空洞、无重叠。
- Zynq 必须发完或整体放弃当前帧后才能开始下一 `frame_id`，禁止主动交错两帧。
- Zynq 应按 `chunk_index` 递增顺序发送；P4 仍必须容忍网络造成的分片乱序和重复。

未来多通道扩展时，载荷采用时间优先交错顺序：

```text
sample0_channel0, sample0_channel1, sample1_channel0, sample1_channel1, ...
```

但 v0.1 的 `channel_count` 必须为 1。

## 13. 当前 8192 点 Profile 示例

当前 Profile 的 CONFIG_SET 显式请求值：

| 字段 | 值 |
|---|---:|
| sample_rate_hz | 4,062,500 |
| frame_sample_count | 8,192 |
| frame_period_us | 50,000 |
| sample_format | 1 |
| channel_count | 1 |
| filter_profile | 1 |

注意：`4,062,500 = 65,000,000 / 16`，它才是传输样点率。

派生参数：

- FFT 栅格：`4,062,500 / 8,192 = 495.91064453125 Hz`。
- 单帧采集窗：`8,192 / 4,062,500 = 2.016492307692 ms`。
- 投递率：`1,000,000 / 50,000 = 20 frame/s`。投递周期与采集窗长度相互独立。

分片结果：

- `chunk_count = 12`。
- chunk 0～10：每包 700 点，`payload_bytes = 1400`，UDP 应用载荷为 1472 字节。
- chunk 11：`sample_offset = 7700`，`samples_in_chunk = 492`，`payload_bytes = 984`，UDP 应用载荷为 1056 字节。
- 单帧 UDP 应用数据共 17,248 字节。
- 按无 VLAN 的 100BASE-TX、计入以太网头/FCS/前导码/帧间隙估算，单帧约 144,320 bit；20 frame/s 约 2.8864 Mbit/s。

## 14. Zynq 发送状态机

1. 上电后进入 IDLE，不发送波形。
2. 收到合法 HELLO 后记录会话、P4 地址和端口，返回 HELLO_ACK；新会话必须清除旧配置。
3. 收到合法 CONFIG_SET 后应用配置、生成 `config_id`，返回 CONFIG_ACK 并进入 READY。
4. 收到 ENABLE_PUSH 后返回 ACK 并进入 PUSH_ENABLED。
5. ADC、低通和抽取链持续运行。每到一个 `frame_period_us` 投递点：
   - 启动或冻结一个含 `frame_sample_count` 个连续、无错序抽取后样点的窗口及全部元数据；可以从该投递点开始收集，也可以从连续环形缓冲选取最新窗口，但同一实现必须固定语义并给出正确时间戳。
   - 若没有足够的新样点或没有可用缓冲，跳过该投递点并增加相应统计，不重复发送旧窗口。
   - 分配新的非零 `frame_id`。
   - 按分片规则顺序发送全部 WAVE_DATA。
   - 完整排入 UDP 层后释放或复用该帧缓冲。
   - 不等待 P4 的波形确认。
6. 收到 DISABLE_PUSH 后按 10.3 节完成停流，再返回 ACK 并进入 READY。
7. 新会话、会话失效或设备复位后回到 IDLE。

采集、DMA 与发送至少使用双缓冲。发送中的缓冲必须不可变，禁止 ADC/DMA 同时覆盖。若软件发送队列可能跨越多个投递周期，不得建立无限积压；宁可统计并丢弃尚未发送的新快照。

## 15. P4 接收与发布状态机

P4 建议预分配 3 个最大帧缓冲：

| 角色 | 用途 |
|---|---|
| assembling | 当前正在重组的 Zynq 帧 |
| latest | 最新完整、尚未被业务层取得的帧 |
| in_use | 分析任务当前持有的不可变帧 |

接收步骤：

1. `recvfrom` 取得一个完整 UDP 数据报。
2. 校验源地址、最小长度、magic、version、session、已知头长和总长度关系。
3. 校验 CRC32。
4. 控制消息交给控制状态机；WAVE_DATA 进入帧重组器。
5. 对 WAVE_DATA 校验分片公式、所有边界、共享元数据和帧级 flags。
6. `config_id` 必须等于最近成功 CONFIG_ACK 的配置身份，采样率、帧长、格式、通道数、投递周期相关状态和滤波配置也必须与 ACK 一致；不匹配的帧作为旧配置数据丢弃。
7. 根据 `frame_id` 处理：
   - 等于 `assembling.frame_id`：接收该分片。
   - 更新：丢弃未完成旧帧，开始重组新帧。
   - 更旧：作为迟到包丢弃。
8. 使用分片位图记录 `chunk_index`。
9. 重复分片：元数据、长度和 CRC 与已记录分片一致时忽略；出现冲突时整帧作废。
10. 收齐全部分片并确认样点区间完整后，将 assembling 原子发布为 latest。
11. 新 latest 可以覆盖尚未 acquire 的旧 latest。
12. 已处于 in_use 的帧不得被网络任务修改。

建议业务接口：

- `acquire_latest(after_frame_id)`：取得更新的最新完整帧；没有新帧时返回空。
- `release(frame_handle)`：业务层使用结束，缓冲重新进入可用池。

接收任务不得执行 FFT、波形绘制或 LVGL 操作，只负责校验、重组、发布和统计。

## 16. 超时、回绕与自动重连

- 默认帧重组超时为 50 ms，从该帧首个有效分片到达开始计时。
- 若超时前出现更新的 `frame_id`，立即丢弃旧 assembling，不必等待计时器。
- `frame_id` 和各消息序号使用 u32 自然回绕。比较新旧时采用模 2³² 规则；差值小于 2³¹ 时才能判定方向。
- P4 可根据 `timestamp_us` 判断数据年龄，避免重复分析长时间未更新的 latest。
- 连续 3 个 STATUS 周期未收到任何合法 STATUS/WAVE_DATA，或发现 `device_boot_id` 改变时，P4 必须将链路标记为离线，停止发布旧测量结果并主动重连。
- 重连时 P4 生成新的非零 `session_id`，清空 assembling/latest，重新执行 `HELLO → CONFIG_SET → ENABLE_PUSH`。旧 in_use 可以由当前分析调用安全释放，但其结果不得再发布。
- 单轮 HELLO 的 3 次 100 ms 重试均失败后，建议每 500 ms 以新会话重试，直到链路恢复；不得等待 Zynq 主动发送 HELLO。

## 17. 帧率控制与百兆链路节流

latest-frame-wins 只控制业务缓存，不能代替发送端限帧率。如果 Zynq 每形成一个 8192 点窗口就立即发送，理论上会接近 496 frame/s，白白消耗网络、P4 CPU 和内存带宽。

v0.1 使用两级节流：

1. `frame_period_us` 控制帧投递率；当前 Profile 固定为 50,000 us，即 20 frame/s。
2. 同一帧内，满 MTU UDP 数据报的发送起点间隔固定为 500 us；不得为了吞吐或镜像诊断缩短该节拍，避免 1G → 100M 交换机端口形成微突发。

当前 12 个分片按 500 us 起点间隔发送，最后一个分片起点约在 5.5 ms；仍远小于 50 ms 投递周期。直连和经交换机两种拓扑都必须抓包实测，不得依赖交换机缓存吸收整帧突发。

## 18. P4 内存与任务要求

1. 按 HELLO_ACK 的 `max_frame_samples` 和 CONFIG_ACK 的实际帧长预分配缓冲，不得每包 `malloc/free`。
2. 当前 8192 点 S16 单帧为 16 KiB，三个样点缓冲共 48 KiB，不含元数据、socket 缓冲和 FFT 工作区。
3. `recvfrom` 缓冲至少为 1472 字节；socket 接收缓存与 lwIP pbuf/mailbox 的联合容量建议不低于 64 KiB，并以零丢包压力测试为准。
4. 网络接收任务优先级必须高于 FFT/分析任务，不能被 UI 或一次完整 FFT 长时间阻塞。
5. FFT 和波形绘制只能消费 in_use 或不可变分析结果，不能直接读取 assembling。
6. 没有空闲 assembling 缓冲时丢弃新帧并增加 `dropped_busy`，不得覆盖 in_use。
7. 接收任务、分析任务和 LVGL 任务之间只传递拥有明确生命周期的数据句柄或副本。

具体核分工和 FFT 要求见配套 Profile。

## 19. 接收端必须维护的统计

- `udp_packets_received`
- `bad_source`
- `bad_magic`
- `bad_version`
- `bad_length`
- `bad_session`
- `crc_failures`
- `config_mismatches`
- `metadata_conflicts`
- `duplicate_chunks`
- `stale_chunks`
- `incomplete_frames`
- `overrange_frames`
- `fifo_overflow_frames`
- `frames_completed`
- `latest_overwrites`
- `dropped_busy`
- `frames_acquired`
- `control_retries`
- `reconnects`

这些计数必须能从串口日志或调试页面读取。只打印一句 `link failed` 然后让人猜，不算诊断能力。

## 20. 安全边界

v0.1 假设 Zynq 与 P4 位于隔离局域网：

- 没有加密、认证或防重放保护。
- `session_id` 只隔离重启和旧数据，不是安全凭证。
- 在共享或不可信网络中，攻击者可以伪造波形和控制报文。
- 首版至少校验固定源 IP、源端口和 `session_id`。

若未来接入普通校园网，应另行增加消息认证，不能把 `session_id` 当密码。

## 21. 联调与一致性测试

### 21.1 基础握手

1. P4 绑定 UDP 50001。
2. P4 生成非零 `session_id` 并发送 HELLO。
3. 验证 HELLO_ACK 的会话、序号、能力位和 `device_boot_id`。
4. 使用第 13 节的显式参数完成 CONFIG_SET，保存返回的 `config_id`。
5. 完成 ENABLE_PUSH。
6. 使用 Wireshark 确认源/目的端口正确、UDP checksum 有效且不存在 IPv4 fragmentation。

### 21.2 样点字节序黄金向量

样点：

```text
[-32768, -1, 0, 1, 32767]
```

对应 S16_LE 字节：

```text
00 80  FF FF  00 00  01 00  FF 7F
```

任一端解出其他数值，都说明它把波形载荷错当成大端了。

### 21.3 完整黄金报文

以下为单分片 WAVE_DATA 报文，共 82 字节。字段取值包括：`session_id=0x11223344`、`message_seq=0x01020304`、`timestamp_us=1234567`、`frame_id=42`、`sample_rate_hz=4062500`、`config_id=7`、`filter_profile=1`、`calibration_id=3`，载荷为上一节 5 个样点。计算 CRC 时偏移 28～31 先置 0，得到 `crc32=0x69DB204C`。

```text
0000: 43 53 4C 50 01 20 00 48 11 22 33 44 01 02 03 04
0010: 00 00 00 00 00 12 D6 87 00 0A 00 0F 69 DB 20 4C
0020: 00 00 00 2A 00 00 00 01 00 00 00 00 00 05 01 01
0030: 00 3D FD 24 00 00 00 05 00 00 01 E8 00 00 00 00
0040: 00 00 00 07 00 01 00 03 00 80 FF FF 00 00 01 00
0050: FF 7F
```

两端单元测试必须能解析该报文并复算出相同 CRC。

### 21.4 测试帧

Zynq 首先发送带 TEST_PATTERN 的确定性图样：

1. 递增斜坡：`sample[n] = (n mod 4096) - 2048`。
2. 固定正弦：频率、幅值、相位和抽取后采样率已知。
3. `frame_id` 每帧递增，`timestamp_us` 单调递增，`config_id` 固定为当前 ACK 值。

P4 校验首尾样点、分片边界、CRC、frame_id、配置身份和样点总数。

### 21.5 故障注入

必须验证：

- 丢弃任意 chunk：整帧不发布。
- 交换两个 chunk 的发送顺序：仍能正确重组。
- 重复发送一个 chunk：只计数，不重复写入。
- 修改 payload 一个字节：CRC 失败，整帧不发布。
- 同一帧某个分片修改共享元数据或帧级 flags：整帧作废。
- 旧帧未完成即到达新 `frame_id`：旧帧被放弃，新帧正常重组。
- 注入旧 `config_id` 帧：不得进入分析任务。
- latest 尚未 acquire 时连续收到新帧：只保留最新完整帧。
- 业务层持有 in_use 时收到新帧：in_use 内容保持不变。
- 重发同键同载荷 CONFIG_SET：返回相同 `config_id`；同键不同载荷：返回 `SEQ_CONFLICT`。
- DISABLE_PUSH_ACK 之后不再从发送端排入旧配置波形。
- Zynq 重启并更换 `device_boot_id`：P4 自动建立新会话并恢复。

### 21.6 压力测试

- 按当前 20 frame/s 连续发送至少 10,000 帧，并额外测试协议允许的最大稳定帧率。
- 分别测试网线直连和经交换机连接。
- 记录 CRC 错误、未完成帧、latest 覆盖、busy 丢弃、重连次数和最大接收任务间隔。
- 验证正式测量中不存在 IP 分片、内存持续下降、UI 卡死或旧配置结果闪回。

## 22. v0.1 冻结项、已验证范围与剩余补证

协议冻结项：

- IPv4 UDP 单播、标准 1500 MTU、固定 1472 字节 UDP 应用载荷上限。
- 32 字节公共头和 72 字节 WAVE_DATA 头。
- 单通道 S16_LE，每个满 WAVE_DATA 数据报 700 点。
- `sample_rate_hz` 表示抽取后的线上样点率。
- `frame_period_us` 独立控制快照投递率。
- `config_id + filter_profile + calibration_id` 标识帧的处理身份。
- 逐数据报 CRC、控制事务幂等缓存和 DISABLE 停流边界。
- P4 完整帧原子发布、latest-frame-wins；WAVE_DATA 无 ACK、无重传、无背压。

当前比赛 Profile 冻结项：

- 单颗 AD9226，仅通道 A，原始 65 MSPS。
- 低通后 16 倍抽取，线上样点率 4.0625 MS/s。
- 8192 点/帧，20 frame/s。

已形成的实现/测量证据：

- `fpga-v1.0.0` 已固定 12 分片、20 frame/s、500 us 发送调度、CRC、控制幂等和停流边界；M8 的 10,001 帧长稳、M12 双目的地归档与 P4 真实 `.2 → .3` 连续 600 帧均已留档。
- P4 的接收缓存、三缓冲所有权、socket/lwIP、ANSI FFT 工作区和 LIVE/STALE 新鲜度门禁已在主机与实板范围内复核；这不把任一历史模拟器结果冒充成真实两板证据。
- FIR 的系数、定点位宽、群延迟、通带/阻带和真实前端干扰范围由 FPGA 证据索引追溯；正式模拟链校准身份为 `25030 / 516 / -6761`，P4 的带内逐频资产为 `C5DCDE41`。
- 直连及实际交换机拓扑的长稳、UDP checksum 与无 IPv4 分片均已有归档；具体证据根、适用范围和镜像身份以 [验收证据索引](../验收证据索引.md) 为准。

仍需补充、但不改变 v0.1 线上格式的边界：

- 若要把 500 us 写成严格物理线上间隔，仍需硬件 RX 时间戳、TAP 或交换机镜像测试仪；普通主机 pcap 时间戳不能单独证明该精度。
- 专家按键到真实 panel flush 的 2 秒证据属于整机/UI 验收，不是本协议的传输格式结论。
