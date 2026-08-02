# CycleScope LAN重放源数据

本目录保存M11正式测量窗口内从Zynq发往电脑的原始经典pcap。每个子目录对应一个
唯一测量点，至少包含：

- `wire.pcap`：`snaplen=0`抓取的完整Ethernet/IPv4/UDP帧；
- `tcpdump.log`：抓包统计，正式点要求内核丢包为0；
- `pcap-analysis.json`：CSLP包数、IPv4分片和UDP checksum独立校验；
- `lan-report.json`：同一窗口的应用层完整性报告；
- `manifest.json`和`SHA256SUMS`：来源、过滤条件、文件大小与哈希。

这些文件只用于后续接收端离线/隔离网络重放。本FPGA任务不操作ESP32-P4。pcap保留
采集时的二层、三层地址和校验和；实际重放前必须在隔离测试网络中确认目标接口、地址
改写和输出限速，禁止直接向未知生产网络发送。

## 归档与版本控制

本目录的原始 pcap、点级报告和哈希清单为可重放输入档案，保持 Git 忽略，避免普通提交携带大批网络载荷。只有本 README 受版本控制；完整目录必须作为一个整体随外部证据归档保存。它证明的是 FPGA 发出的历史输入包及其完整性，不是 ESP32-P4 的最终测量输出。
