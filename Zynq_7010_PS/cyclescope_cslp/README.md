# CycleScope Zynq CSLP

这是 AD9226 正式链路的 PS 端源码事实源。旧 `lwip_udp_perf_server` 中的
AD7608/AD9744 业务代码不参与本应用构建。

## 边界

- `include/`、`src/`：无 Xilinx 依赖的 CSLP v0.1 编解码、CRC、控制幂等缓存和双缓冲所有权状态机。
- `target/`：Zynq-7010 的 AXI DMA、AXI GPIO、lwIP RAW API 与节流调度适配。
- `tests/`：主机黄金报文、CRC、固定 12 分片、控制状态机和所有权测试。
- `scripts/build_vitis.py`：从 M4 XSA 重建 Vitis 2025.1 platform/BSP/application；生成物只进入 `build/`。
- BSP 构建后会断言 UDP 开启、DHCP/IPv6/IP 分片关闭，并确认 Zynq GEM 的 TX/RX checksum offload 生效。

## 验证

```bash
make test
make adc-analysis-test
make vitis
make vitis-lan-test
```

这些命令只运行主机测试或重建软件，不会生成 bitstream、下载板卡或自行发送真实
UDP。bitstream 由 PL 目录单独构建；真实 AD9226、UDP 抓包和首样点硬件时间戳
属于 LAN-only 板级验收。SPI 已退出正式范围，不启用、不测试、不维护。系统
Python 缺少 NumPy 时，`make test` 会跳过 ADC 分析器
用例；M7 标定改动必须另外执行 `make adc-analysis-test`，由 WaveBench 虚拟环境完成。

默认 `make test` 只运行 CSLP、ADC 捕获/分析、LAN stress 和 pcap 测试；遗留
`test_spi_protocol.py` 保留为历史资产，但不会被默认测试发现或执行。

### M11校准身份构建

默认固件保持未校准语义：`calibration_id=0`、`CALIBRATED=0`、
`scale_uV_per_lsb=488`、`offset_uV=0`。非零校准不得用松散环境变量直接拼数值；只能
通过`CSLP_CALIBRATION_MANIFEST`指向一份
`CycleScope M11 validated calibration build manifest v1`清单。构建脚本会逐文件验证
以下四份产物的相对路径、大小和SHA-256：

- `calibration.json`
- `response.csv`
- `uncertainty.json`
- 独立保留点报告

清单还必须证明至少7个保留点全部通过、最大绝对误差不超过5 mV，并绑定当前M11
matrix manifest。任一条件缺失、路径逃逸或哈希变化都会在Vitis构建前失败。测试图样
固件在编译期禁止携带真实ADC的非零校准ID。

校准冻结后仅构建、不会自动下载：

```bash
CSLP_PEER_IPV4_LAST_OCTET=4 \
CSLP_CALIBRATION_MANIFEST=/absolute/path/to/calibration-build-manifest.json \
make vitis
```

电脑端验收必须显式声明同一组身份，不能只看到`CALIBRATED`就放行：

```bash
python3 tools/cslp_lan_stress.py \
  --source-mode real-adc \
  --expected-calibration-id 17 \
  --expected-scale-uv-per-lsb 516 \
  --expected-offset-uv -6708 \
  --frames 21 --report reports/calibrated-smoke.json
```

上述数值只是命令格式示例，不是本板当前校准结果。未提供非零ID时，LAN工具维持旧
行为，只接受`calibration_id=0`且`CALIBRATED`清除；可额外显式门禁名义比例和偏置。

`vitis-lan-test` 是电脑端 LAN 诊断构建：启用 PL 测试源，并仅接受
`192.168.10.4` 的控制请求。默认图样是 ramp；也可在构建前设置
`CSLP_TEST_MODE=ramp|sine|multitone`、`CSLP_TEST_AMPLITUDE=0..2047`、
`CSLP_TEST_BIN=1..1008` 和 `CSLP_TEST_FAULTS=0..7`。sine 的 bin 是 8192 点、
4.0625 MS/s 输出帧的相干 DFT bin。普通 `vitis` 仍关闭测试图样、fault mask 为 0，
并使用 Profile 默认对端 `192.168.10.3`，诊断配置不会污染正式固件。

诊断故障不会在首帧前偷偷发生：PS 等首帧完整上传后再翻转 PL one-shot 控制位，
因此 OTR 恰好标记下一接受帧，overflow 从下一帧起 sticky，frame-drop 则在相邻
已发布帧的硬件时间戳中留下唯一约 `100 ms` 间隔。PL 累计 drop 也会汇入 CSLP
STATUS，不能只在内部 `status_word` 里自娱自乐。

每帧的 CSLP `timestamp_us` 来自 PL 锁存的首输出样点 ADC tick。RTL 已按三级 FIR
精确补偿 `694` 个 65 MHz tick；PS 在 capture 开启前用单调时钟夹取 PL ADC tick
建立锚点，再以商/余数算法换算微秒。时间戳读取失败会丢弃该帧并增加 metadata
failure，禁止退回“DMA 完成时间减固定延迟”的静默估算。

板级链路固定为 `100 Mb/s`：BSP 使用 `CONFIG_LINKSPEED100`，绕开 AMD 通用
PHY 自动测速；应用内的 RTL8211F 专用后端仍启用自协商，但只在标准 page 0
广告 `100BASE-TX Full`，并反读 PHY ID 与 PHYSR 确认 100M/全双工/链路。
它必须与 XSA 中 PS7 GEM0 的 IO PLL `/8/5`、25 MHz 默认配置配套。交换机端口
应保持自协商；不要把任何一侧强制成“关闭自协商的 100M”，否则会有双工误判风险。

PHY 只依赖板卡冷上电复位。当前实板禁止通过 MIO47 脉冲复位：该操作会使
RTL8211F 在 MDIO 地址 0～31 全部返回 `0xFFFF`，只能彻底断电后恢复。若以后
追查该管脚，必须同时测量 PHY pin 12、电源和晶振波形，不能拿 PS GPIO 回读代替。

电脑和 Zynq 分别接入交换机，因此电脑网卡的协商结果与 Zynq 链路速率无关。
Zynq 端验收只看 PHY ID（`0x001c/0xc91x`）、PHYSR（100M/全双工/link）、
GEM0 25 MHz 时钟和 `NWCFG`；电脑端只负责控制、收包、抓包和接收丢包统计。
启动日志可用以下结构化标记诊断 PHY/GEM；电脑侧测试不依赖 UART，因为应用只有
在这三组内部门禁全部通过后才会启动 UDP 服务：

```text
CYCLESCOPE_RTL8211F_ID_PASS ADDR=1 ID1=0x001c ID2=0xc916
CYCLESCOPE_RTL8211F_100_FULL_PASS PHYSR=0x????
CYCLESCOPE_GEM0_100_FULL_PASS SLCR=0x00500801 NWCFG=0x???????? SRC=0 DIV0=8 DIV1=5
```

`PHYSR` 的其他状态位可能改变，但 `bits[5:4]=01`、duplex bit 3 和 link bit 2
必须同时成立。应用另有独立 PHY-ready 门禁，避免 AMD `xemac_add` 在 PHY 初始化
失败后仍返回成功所造成的假阳性。

测试图样烟测默认接收 1,200 帧（约 60 秒）：

```bash
python3 tools/cslp_lan_stress.py \
  --frames 1200 \
  --report reports/lan-smoke.json
```

图样测试应同时保存完整帧，再用独立分析器验证码域或相干谱线：

```bash
python3 tools/cslp_lan_stress.py \
  --source-mode test-pattern \
  --frames 21 \
  --capture-dir reports/sine-bin256/capture \
  --report reports/sine-bin256/lan.json

../../../tools/wavebench/.venv/bin/python tools/cslp_test_pattern_analyze.py \
  --mode sine --amplitude 1600 --coherent-bin 256 \
  --capture reports/sine-bin256/capture \
  --lan-report reports/sine-bin256/lan.json \
  --report reports/sine-bin256/analysis.json
```

故障 ELF 必须显式告诉接收端预期掩码；掩码与 `CSLP_TEST_FAULTS` 一致：bit0 OTR、
bit1 overflow、bit2 frame-drop。默认 0 仍对任何异常 fail-closed：

```bash
python3 tools/cslp_lan_stress.py \
  --source-mode test-pattern \
  --expected-test-faults 4 \
  --frames 10 \
  --capture-dir reports/fault-drop/capture \
  --report reports/fault-drop/lan.json
```

真实 AD9226 上板烟测必须显式选择 ADC 源模式，避免拿测试图样规则误判真实数据：

```bash
python3 tools/cslp_lan_stress.py \
  --source-mode real-adc \
  --frames 21 \
  --run-timeout 10 \
  --report reports/adc-smoke.json
```

该模式仍严格检查 CSLP、分片、序号、帧号和状态计数，但允许先完成携带
`ADC_OVERRANGE`/`FIFO_OVERFLOW` 的帧重组，以便报告 `sample_min`、`sample_max`、
`sample_span` 和 `sample_unique_values`；这些硬件状态最终仍会令测试失败，不能被
“确实收到了样点”掩盖。全程恒定样点同样判失败。

### M7 ADC 标定证据

`cslp_lan_stress.py` 可把严格重组后的每个 8192 点帧独立保存；帧之间是
50 ms 投递快照，不能拼接成连续 4.0625 MS/s 波形。输出目录采用 fail-closed
语义，已存在时拒绝覆盖：

```bash
python3 tools/cslp_lan_stress.py \
  --source-mode real-adc \
  --frames 21 \
  --capture-dir reports/adc-point \
  --report reports/adc-point-lan.json
```

零输入允许只有一个码值，但其余链路门禁不放松：

```bash
python3 tools/cslp_lan_stress.py \
  --source-mode real-adc \
  --frames 21 \
  --activity-policy allow \
  --overrange-policy reject \
  --capture-dir reports/adc-zero \
  --report reports/adc-zero-lan.json
```

真实 ADC 的 `--overrange-policy` 默认为 `reject`；OTR 专项可选择 `require`，此时至少
一帧必须置位，且 WAVE 帧数必须与 STATUS 增量一致。`allow` 只允许出现
OTR，仍要求两处计数一致。内部注入则使用 `--expected-test-faults`，不能借用
`allow` 放宽；未声明的 FIFO、CRC、帧号、NIC 和 drop 始终严格为零。

离线频谱/标定工具使用 WaveBench 虚拟环境中的 NumPy：

```bash
../../../tools/wavebench/.venv/bin/python tools/cslp_adc_analyze.py tone \
  --capture reports/adc-point \
  --lan-report reports/adc-point-lan.json \
  --scope-npy reports/wavebench-raw/ch1.npy \
  --expected-frequency-hz 100000 \
  --report reports/adc-point-analysis.json
```

工具支持 `zero`、`tone`、`square` 和 `sweep`。`tone` 会输出频率、
Vpp、RMS、THD、SFDR 和候选 `scale_uV_per_lsb`；只有多点 `sweep`
相对 RTM 参考的幅值残差全部不超过 5 mV，候选比例才通过幅值门禁。
`tone --expected-frequency-hz` 表示 65 MS/s ADC 端的原始输入频率，可取
`0 < f < 32.5 MHz`；高于 4.0625 MS/s 输出 Nyquist 的点必须使用
`--response-only`。此时 RTM 仍在原始输入频率估计，ADC 则在按 4.0625 MHz
折叠后的频率拟合。折叠到 DC/Nyquist 附近的退化点会被拒绝。

`sweep` 只接受不高于 500 kHz 的通带点和不低于 1 MHz 的阻带点。通带幅值采用
逐帧 `median`；阻带采用较保守的逐帧 `p95`，只用 `1e-12 code Vpp` 数值下限
避免 `log(0)`，不作量化或 zero 噪底扣除，也不以此放宽 50 dB 门禁。zero、tone
和 square 标定各自至少需要 21 个完整帧。

正式长稳压测使用 `--frames 10000 --run-timeout 540`。工具严格验证控制幂等、
CSLP CRC、12 分片布局、序号/帧号连续性、所选 source mode 标志、STATUS 计数、
DISABLE 排空边界和主机 NIC 丢包。它验证的是 Profile 固定 20 frame/s 的完整
业务链路（约 2.9 Mbit/s），不能冒充百兆或千兆线速饱和测试。

socket 结果不能证明 IPv4 未分片、UDP checksum 非零或线上分片间隔；烟测和长稳
都应同步抓 pcap：

```bash
sudo tcpdump -i enp2s0 -s 0 -U -Z feisibo \
  -w reports/lan-smoke.pcap \
  'host 192.168.10.2 and (arp or udp)'

python3 tools/cslp_pcap_analyze.py reports/lan-smoke.pcap \
  --lan-report reports/lan-smoke.json \
  --tcpdump-log reports/lan-smoke-tcpdump.log \
  --report reports/lan-smoke-pcap.json
```

分析器逐包复算 IPv4/UDP checksum，拒绝零 checksum、坏 checksum、IPv4 分片、
截断包、tcpdump 内核丢包和 pcap/LAN WAVE 包数不一致。pcap 与板端 GEM
RX/TX/FCS/symbol 计数属于独立验收证据，不能由主机 NIC 统计替代。
当前板端使用 500 us 的 WAVE 分片调度裕量。普通电脑网卡若只有 host 软件时间戳，
长抓包可能因 NAPI 批处理把少量相邻包标成近乎同刻；这种 pcap 仍可验证分片和
checksum，但严格的线上起点间隔需要硬件 RX 时间戳或交换机镜像测试仪。

Vitis 2025.1 的 AMD lwIP 端口有两个与本 Profile 冲突的生成缺陷：
`IP_FRAG=0` 会被模板生成为 `#undef`，GEM DMA 又会把 1514-byte 标准帧截成
1500 byte。`scripts/build_vitis.py` 只在临时 workspace 中对精确匹配的 2025.1
源码打补丁，并要求最终 `lwipopts.h` 明确定义 `IP_FRAG/IP_REASSEMBLY=0`。
升级工具链时补丁不匹配会中止构建，不能静默沿用。
