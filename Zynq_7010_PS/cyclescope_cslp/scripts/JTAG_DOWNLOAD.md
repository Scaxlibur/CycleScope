# JTAG 临时下载入口

`jtag_download.tcl` 只做易失性的 JTAG 下载，不写 QSPI/启动介质。默认运行仅校验
bitstream、XSA 生成的 `ps7_init.tcl` 和 ELF，并打印操作计划，不连接硬件。

先在一个终端启动 Vivado 2025.1 `hw_server`：

```bash
/tools/Xilinx/2025.1/Vivado/bin/hw_server -L- -stcp::3121 -p0
```

另一个终端先审计默认输入：

```bash
/tools/Xilinx/2025.1/Vitis/bin/xsdb -no-ini \
  Zynq_7010_PS/cyclescope_cslp/scripts/jtag_download.tcl
```

确认输出中的路径、Digilent 序列号和执行顺序后，才显式允许下载：

```bash
/tools/Xilinx/2025.1/Vitis/bin/xsdb -no-ini \
  Zynq_7010_PS/cyclescope_cslp/scripts/jtag_download.tcl \
  --execute
```

脚本只接受序列号默认为 `210241398254` 的 Digilent cable，并要求 `APU`、
`xc7z010`、`ARM Cortex-A9 MPCore #0` 分别唯一匹配。完整顺序为：系统复位并停核、
下载 bitstream、在强制内存访问保护下执行 `ps7_init`/`ps7_post_config`、下载 ELF、
恢复原访问配置并继续 A9#0。脚本还会拒绝在 `main` 之外的分支运行。

需要覆盖产物路径或远端 `hw_server` 时使用：

```bash
/tools/Xilinx/2025.1/Vitis/bin/xsdb -no-ini \
  Zynq_7010_PS/cyclescope_cslp/scripts/jtag_download.tcl \
  --bit /absolute/path/system.bit \
  --ps7-init /absolute/path/ps7_init.tcl \
  --elf /absolute/path/application.elf \
  --hw-url tcp:127.0.0.1:3121 \
  --cable-serial 210241398254
```

以上仍是默认 dry-run；路径存在不等于获得了下载授权，必须另加 `--execute`。

如果此前运行过 MIO47 PHY 复位实验，下载前必须让板卡彻底断电，等待电源放净后
再上电；`rst -system` 不能恢复已经在 MDIO 上失联的 RTL8211F。当前下载流程
不得再驱动 MIO47。
