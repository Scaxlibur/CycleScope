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
make vitis
```

当前里程碑只允许主机测试与软件编译。命令不会生成 bitstream、下载到板卡或发送真实 UDP。
真实 AD9226 数据、UDP 抓包、SPI 并发和时间戳校准统一属于 M6 板级验收。
