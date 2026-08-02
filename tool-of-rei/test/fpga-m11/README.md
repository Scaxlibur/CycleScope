# M11 WaveBench 执行入口

本目录只服务于 M11。正式链路、幅度矩阵和验收门槛见上一级
`M11-真实全链路FIR与信号处理压力测试计划.md`。

## 安全边界

- DG4202 CH1 必须保持 50 Ω负载语义；本目录没有修改负载的入口。
- RTM2032 CH1/CH2 必须同时保持高阻；任何一个通道门禁失败都停止。
- DP800 绝对只读；只允许 ID、status、measurement 和 protection status。
- M11 配置在内存中由现有 0600 私有配置派生，`max_source_vpp` 固定覆盖为
  `0.5 Vpp`；不会复制或公开实验室 resource。
- WaveBench 仓库保持只读，本目录不修改其源码、配置或插件。
- 每个正式LAN窗口同步用`tcpdump`保存Zynq→电脑的完整Ethernet/IPv4/UDP经典pcap；
  原点留在point evidence，并按大小与SHA-256复制到`tool-of-rei/source_data/<point>/`。
  pcap必须通过WAVE包数、非零有效UDP checksum、零IPv4分片和零内核丢包门禁。

## 当前命令

```bash
cd /home/feisibo/git-projects/CycleScope/CycleScope-FPGA

# 完全离线：配置、plan、capability、零写和证据路径检查
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_wavebench_safe.py check

# 完全离线：逐个让 WaveBench arb-load --dry-run 验证当前matrix-v3的27个ARB
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_arb_dry_run.py \
  --matrix tool-of-rei/evidence/m11-real-frontend-20260731/offline/matrix-v3 \
  --output tool-of-rei/evidence/m11-real-frontend-20260731/offline/arb-dry-run-v5

# 仅只读连接：DG/RTM/DP800 身份与完整安全状态
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_wavebench_safe.py preflight-readonly

# 仅当只读预检的唯一失败是“DG输出仍为ON”时，执行一次受控OFF；不恢复为ON
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_wavebench_safe.py safe-source-off \
  --acknowledge M11_DG_CH1_50OHM_OUTPUT_OFF_NO_RESTORE_TO_ON

# DG全程OFF：RTM CH1+CH2同次采集，并行保存至少64个真实ADC LAN帧
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_wavebench_safe.py zero-live \
  --acknowledge M11_WIRING_DG50_RTM12_HIGHZ_DP800_READONLY \
  --frames 64 --profile noise-500k \
  --expected-calibration-id 25030 --expected-scale-uv-per-lsb 516 \
  --expected-offset-uv -6761

# DG全程OFF：500 kHz以上至当前采样Nyquist的自激/固定杂散筛查
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_wavebench_safe.py zero-live \
  --acknowledge M11_WIRING_DG50_RTM12_HIGHZ_DP800_READONLY \
  --frames 64 --profile hf-spur \
  --expected-calibration-id 25030 --expected-scale-uv-per-lsb 516 \
  --expected-offset-uv -6761

# 零输入正式点的强制前置门：仅做2帧CSLP握手/收包，不触发RTM
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_wavebench_safe.py lan-preflight \
  --acknowledge M11_WIRING_DG50_RTM12_HIGHZ_DP800_READONLY \
  --expected-calibration-id 25030 --expected-scale-uv-per-lsb 516 \
  --expected-offset-uv -6761

# C阶段点的完全离线检查；v2用户授权门禁PASS后可进入正式点
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_sine_point.py check \
  --case-id c-100k-100mVpp

# 只读重分析既有闭合点；先验证原点SHA256SUMS，不覆盖原点或既有输出目录
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_sine_point.py reanalyze \
  --point-dir tool-of-rei/evidence/m11-real-frontend-20260731/points/<point-dir> \
  --output-dir tool-of-rei/evidence/m11-real-frontend-20260731/offline/<new-analysis-dir>

# E阶段两步冻结：fit只能读取36个训练case，validate才允许读取7个holdout
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_calibration.py fit \
  --output-dir tool-of-rei/evidence/m11-real-frontend-20260731/offline/<new-fit-dir>
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_calibration.py validate \
  --fit-dir tool-of-rei/evidence/m11-real-frontend-20260731/offline/<frozen-fit-dir> \
  --output-dir tool-of-rei/evidence/m11-real-frontend-20260731/offline/<new-calibration-dir>

# 非零校准固件的LAN逐帧身份＋pcap验收
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_calibrated_lan_smoke.py \
  --manifest tool-of-rei/evidence/m11-real-frontend-20260731/offline/calibration-v1/calibration-build-manifest.json \
  --frames 22

# F/I阶段从离线check开始就必须显式提供已验证manifest；缺失时在仪器I/O前拒绝
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_sine_point.py check \
  --case-id f-fixed-1e+06Hz \
  --calibration-manifest tool-of-rei/evidence/m11-real-frontend-20260731/offline/calibration-v1/calibration-build-manifest.json

# F阶段23点全部完成后生成不可覆盖的保守衰减汇总
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_fir_stopband_summary.py \
  --output-dir tool-of-rei/evidence/m11-real-frontend-20260731/offline/fir-stopband-summary-v1

# G/H/I ARB从离线check开始就强制绑定非零校准manifest
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_arb_point.py check \
  --case-id g-b-low-low-crest \
  --calibration-manifest tool-of-rei/evidence/m11-real-frontend-20260731/offline/calibration-v1/calibration-build-manifest.json

# G阶段受控ARB live；G点入口会自动提高到至少64个完整帧
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_arb_point.py arb-live \
  --case-id g-b-low-low-crest --frames 64 \
  --acknowledge M11_DG50_RTM12_HIGHZ_DP800_READONLY_FRONTEND_PHYSICAL_GATE \
  --stage-acknowledge M11_STAGE_G_CONTEST_MULTITONE \
  --calibration-manifest tool-of-rei/evidence/m11-real-frontend-20260731/offline/calibration-v1/calibration-build-manifest.json

# G阶段10点完成后从原始证据重算不可覆盖汇总
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_multitone_summary.py \
  --output-dir tool-of-rei/evidence/m11-real-frontend-20260731/offline/multitone-summary-v1

# H阶段15点完成后从原始证据重算组合恢复与干扰抑制汇总
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_combination_summary.py \
  --output-dir tool-of-rei/evidence/m11-real-frontend-20260731/offline/combination-summary-v1
```

`preflight-readonly` 不执行 scope capture，不切换 DG 输出，不改变任何电源设置，也不
控制 FPGA。其证据写入 `tool-of-rei/evidence/m11-real-frontend-20260731/preflight/`。

`noise-500k`固定使用5 ms窗口和20 mV/div，要求CH1/CH2前后均为`DCL`高阻，计算
0～500 kHz积分噪声与最大离散杂散；`hf-spur`固定使用10 us窗口和20 mV/div，筛查
500 kHz以上高频分量。两者都与至少64帧真实ADC LAN数据同窗，并将WaveBench
`data/raw`原生包按大小和SHA-256复制到点证据。入口不会恢复RTM时基/垂直量程，
不会修改coupling；DG始终OFF，DP832始终零写。按名义4.515984倍得到的输入等效值
只作临时参考，实测`Gamp`和探头修正完成前不得冒充正式校准。

`m11_sine_point.py`的live入口支持矩阵中的C/D/E/F/I单频点，并为每个阶段要求不同的
明确确认串；阶段上限分别为`0.1/0.45/0.45/0.2/0.2 Vpp`。它先用内存中0.5 Vpp
配置执行WaveBench plan check/verify/plan，plan只在DG OFF时设置SIN/幅值/频率，
绝不包含source ON或任何DP832写；随后单独执行一次受控ON窗口，并行完成RTM
CH1+CH2同次采集和真实ADC LAN帧，最后在`finally`中执行一次OFF。WaveBench原生
run/raw按大小与SHA-256复制到point evidence。正式LAN窗口还同步生成完整pcap，复制到
`tool-of-rei/source_data`；ESP32-P4不在本任务范围。物理门见`M11-A现场确认.md`。

示波器定量结论固定以WaveBench原始`ch1.npy/ch2.npy`、metadata质量摘要和
`wavebench.data.fft.analyze_fft`为主；已知频率五谐波最小二乘仅作幅值/相位交叉校验，
两者幅值差超过2%即失败。截图只用于查看削顶、振铃等视觉异常，不从像素或屏幕标注
读取正式数值。`reanalyze`会生成独立的`analysis-wavebench-primary.json`并保留原点
不变，便于确认实时分析可重复。

旧`100 kHz / 20→50→100 mVpp` provisional序列保持原结论，全部为
`formal_calibration_eligible=false`、`calibration_id=0`。用户已明确允许忽略反馈取样
位置、探头交换修正和内部电源轨/温度，并确认双通道按`1×`读数；v2 physical gate
据此PASS，但仍不把这些未表征项伪装成测量结果。C阶段六个正式点已经完成。

D阶段已完成`10/100/200/500 kHz × 10/50/100/250/450 mVpp`。10 kHz的500 mV
非赛题余量点超过CH1/CH2正常门，故所有后续阶段的执行上限冻结为0.45 Vpp；500 mV
case仍留在离线矩阵中绑定失败证据，但live入口会在任何仪器I/O前拒绝。动态范围、
压缩、THD、SFDR和MAD离群点汇总可用：

```bash
/home/feisibo/git-projects/CycleScope/tools/wavebench/.venv/bin/python \
  tool-of-rei/m11/m11_dynamic_range_summary.py \
  --output-dir tool-of-rei/evidence/m11-real-frontend-20260731/offline/<new-dir>
```

最终事实源为`offline/dynamic-range-summary-v2/`；v1因补充分析器接收`int16`后在
非验收字段`rms_total`触发平方溢出warning而保留为中间记录，不用于结论。v2在分析
边界显式转为`float64`，仍不删除、不替换任何原始ADC样点。

E阶段已完成：36个训练case先冻结`offline/calibration-fit-v1/`，fit读取holdout数为0；
随后7个独立holdout全部PASS，最大绝对误差`0.173011 mV`、RMS`0.116103 mV`。
`offline/calibration-v1/`生成四份正式产物和构建清单；Vitis 2025.1真实ADC peer `.4`
ELF已用JTAG易失下载，板上23帧逐帧匹配`ID=25030/516 µV/code/-6761 µV`及
`CALIBRATED`标志。完整证据见`firmware/calibrated-peer4-v1/`。

当前LAN入口不再把ID0写死：C/D/E历史点不提供manifest时仍显式要求
`ID=0/488/0/CALIBRATED=0`；F/I必须提供已验证manifest并逐帧要求非零身份，否则在
任何仪器I/O前fail closed。CSLP metadata仍只有标量；完整频响在`response.csv`，固件
没有逐频逆补偿。契约证据见`../evidence/m11-calibration-contract-20260731/`。

F阶段已完成15个固定点和8个Q1.17最坏残余点。最终汇总逐点从归档NPY重算
`wavebench.data.fft.analyze_fft`，固定栅格点用2%交叉校验，落在最近半个FFT bin内的
非相干点允许10%已知频率拟合交叉校验；拟合不替代Hann FFT主幅值。ADC残余按最终
折叠频率逐帧拟合并取经验p95，不减噪声、不改样点。最差衰减下界为`72.3376 dB`，
23/23均通过50 dB门槛；事实源为`offline/fir-stopband-summary-v1/`。

G阶段已完成10点/650帧。示波器全局谱固定调用WaveBench归档NPY的`analyze_fft`；
由于weak-line明确允许“基波不是最大分量”，全部manifest谱线另用已知频率联合拟合，
不能把最大bin偷换成基波。最大谱线/Vpp/真RMS误差为`0.693/0.912/0.396 mV`，全部
通过3 mV目标；最大带内残余互调p95为`0.05475 mVpeak`，0个离群点。事实源为
`offline/multitone-summary-v1/`。

DG4202标准插件只能从basic函数上传一次ARB。WaveBench仓库仍保持只读；M11重复上传
扩展位于`tool-of-rei`，绑定`wavebench-rigol-dg4000 1.1.0`及驱动源码SHA-256，只允许
已由preflight确认的USER/OFF快照，其他编码、上传、回读和失败恢复仍使用原插件。
用户已明确取消旧USER波形恢复门禁；I首个单频曾由checked WaveBench plan在输出OFF时
执行USER→SIN作为运行配置，不使用裸SCPI，也不恢复旧易失USER波形；该动作不进入
最终验收。

H阶段已完成15点/975帧。三组`u_b`分别与1/1.5/2/2.5/3 MHz的200 mVpp干扰组合，
最差抑制下界`74.8455 dB`；最大谱线/Vpp/真RMS误差`0.444/0.704/0.245 mV`，均通过
3 mV目标，最大带内残余互调p95`0.1086 mVpeak`、0个离群点。事实源为
`offline/combination-summary-v1/`。

2026-08-01，I阶段已完成5个200 mVpp单频（4/5/7.2/7.5/10 MHz）和2个B-edge组合
（5/10 MHz），共7点/287帧，自动重试与已通过点重复均为0。最差抑制下界
`65.996983 dB`；组合最大谱线/Vpp/真RMS误差为
`0.331668/0.554865/0.110501 mV`。7.2 MHz点因RTM把时基量化为2 µs，仅从原始NPY
选择3125点、9周期的最长整数周期前缀调用WaveBench原生FFT；没有重测、插值、删除或
替换样点。事实源为`offline/upper-frequency-summary-v1/summary.json`，SHA-256为
`ea5a82759e050e8e2db8fa78aa5dde6b7cde935e76156cc8b9b3e8de880375cd`。

J阶段已完成一次连续长稳：请求10,000帧，严格实收10,001帧和120,012个WAVE包；首
完整帧延迟`58,437.749 µs`，约第100/5000/9800帧三次RTM双通道同窗均PASS，十个
1000帧块的增益跨度`0.001907588 dB`，ADC离群点0，GEM/NIC/pcap错误为0。完整pcap
保存在`tool-of-rei/source_data/20260801_022046_539376+0800_j-response-longrun/`。
最终事实源为`offline/j-longrun-final-v1/summary.json`，SHA-256为
`9dcf742786dffbf25815df7bbbb2b449470963681c178bb0c18b9fa3d26128d4`。

J原始点保留`pass=false`：约512秒的长寿命DG TCP会话在结束OFF时失效；测量门本身
全部PASS。随后关闭旧transport，经checked fresh session只执行一次输出OFF并确认
`USER/OFF`，没有重跑任何长稳帧。该安全处置是`USER/ON → USER/OFF`，与用户明确
豁免的`USER/OFF → SIN/OFF`函数模式恢复不同。后者完全不进入M11门禁，不要求执行，
也不影响最终PASS/FAIL；最终摘要中的`final_fixed_sine_restoration_required=false`即为
机器可读口径。

A～J至此全部冻结。后续不得为补做函数模式恢复或清理历史失败点而重跑；如有新目标，
从驾驶舱和上述两个最终摘要恢复上下文。
