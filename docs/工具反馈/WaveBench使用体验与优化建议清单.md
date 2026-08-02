# WaveBench 使用体验与优化建议清单

## 1. 文档范围

本文基于 CycleScope 开发期间对 DG4202、RTM2032 和 DP800 的真实使用经历整理，重点覆盖：

- 信号源与示波器联调；
- 多频点、升降频、不同幅度的系统标定；
- 谐波和任意波测试；
- 截图、波形、测量值和命令日志留证；
- 人工操作仪器与自动流程并存的现场环境；
- 失败停止、状态恢复、断点续跑和最终审计。

当前 WaveBench 已经具备比较扎实的基础：显式命令、能力门禁、高阻保护、run plan、失败包保留、命令日志、截图和波形采集都很有价值。下面的建议不是推翻现有设计，而是让它从“可靠的仪器控制 CLI”进一步变成“能长期承担复杂联调与标定的实验台”。

总体判断：这次最不顺手的地方并不是少了几个 SCPI 命令，而是**仪器状态所有权、能力降级、完整恢复和大型标定流程仍需要外部夹具兜底**。

## 2. P0：优先解决的可靠性问题

### [ ] 1. 增加跨进程资源锁和实验台租约

**实际不便**

当前文档已经明确说明同一物理 resource 没有跨进程锁。开发期间人工操作 DG4202 与自动命令曾发生交错：一次命令刚确认输出为 OFF，下一次操作前仪器状态已经被人工改动。单条命令本身没有错，但多个 one-shot session 之间没有状态所有权，容易产生误判。

**建议**

- 按 VISA resource 建立跨进程锁，Linux/WSL 可使用文件锁，锁文件中记录 PID、启动时间、计划名称和只读/读写模式；
- `run plan` 默认持有整个实验周期的独占写租约；
- 只读命令可以共享锁，但不得与会改变响应流或错误队列的操作并发；
- 提供 `wavebench lock status` 和带明确风险提示的 stale-lock 清理命令；
- 外置插件和内建驱动必须走同一套锁，不能由插件绕过。

**验收标准**

- 进程 A 持有 DG4202 写租约时，进程 B 在发送任何 SCPI 前即被拒绝；
- 锁冲突输出 resource、持有者和建议动作；
- 异常退出后能识别并安全回收 stale lock。

### [ ] 2. 为写操作增加状态指纹和“比较后执行”语义

**实际不便**

资源锁只能约束软件进程，挡不住人直接按仪器面板。标定现场真正需要的是：如果输出、负载、函数、频率或幅度在两步之间被人工改变，流程应立即停止，而不是继续使用已经失效的前提。

**建议**

- 预检后生成关键状态指纹，例如 `output/function/frequency/amplitude/load/offset/mode`；
- 写步骤可声明 `expected_state` 或 `expected_state_hash`；
- 每个危险步骤前重新回读并比较，发生漂移时 fail-closed；
- 日志明确区分“WaveBench 改变”“仪器自行改变”“外部/人工改变”；
- 提供 `allow_external_change = false` 的 plan 级默认值，只有显式步骤才能接受状态变化。

**验收标准**

- 预检后人工打开输出，下一写步骤必须在写入前停止；
- 失败包保留旧状态、新状态、差异字段和时间戳；
- 不允许仅凭最后一次命令成功就推断仪器仍处于目标状态。

### [ ] 3. 增加核心层“只读仪器”策略，而不只依赖操作习惯

**实际不便**

CycleScope 标定期间 DP800 被明确要求绝对禁止写入。现有流程只能靠调用方不执行写命令，再通过命令日志证明写入次数为零。对高风险设备而言，这种约束太软。

**建议**

- 配置支持 `access = "read_only" | "read_write" | "disabled"`；
- 在 Service/dispatcher 层拦截写操作，不能只由具体驱动自觉遵守；
- run artifact 固定记录每台仪器的 query/write/binary-write 计数；
- 报告中直接给出 `network_writes=0` 或 `instrument_writes=0` 结论；
- 可进一步支持命令白名单，例如只允许 `*IDN?`、状态和测量查询。

**验收标准**

- `read_only` 的 DP800 对 `power set`、`power output` 和插件写入口全部拒绝；
- 拒绝发生在打开写事务前，并有稳定错误码；
- 最终报告无需额外脚本即可证明写入次数为零。

### [ ] 4. 让 `scope status` 支持能力降级，而不是全有或全无

**实际不便**

在 RTM2032 上执行 `scope status` 时，命令因为驱动没有声明 `scope.snapshot` 而以 exit 2 结束。仪器本身在线，波形采集也能工作，但“status”这个名字给人的预期是至少返回可读的基础状态。能力缺失与仪器故障使用相似的失败外观，现场很容易误判。

**建议**

- 把 `scope status` 改为聚合式、可降级的只读摘要；
- 返回当前驱动能读到的 IDN、耦合、采集状态、通道状态等字段；
- 对缺失字段返回 `unsupported`，并列出缺少的 capability；
- 保留严格命令，例如 `scope snapshot --require-complete`；
- 错误信息给出当前 driver ID、版本、已有能力、缺失能力和可用替代命令。

**验收标准**

- RTM2032 即使没有 `scope.snapshot`，`scope status` 仍能返回基础在线状态；
- `unsupported` 不与 `timeout`、`offline`、`SCPI error` 混为一谈；
- 人和自动夹具都能根据结构化状态判断“仪器故障”还是“插件能力不足”。

### [ ] 5. 提供完整信号源事务和可验证恢复

**实际不便**

现有 basic restore 只覆盖输出、函数、频率、Vpp 和部分方波占空比，不覆盖 offset、phase、load、polarity、frequency mode、burst、modulation、marker、harmonic、ARB 易失内存等。实际谐波/ARB 测试只能由外部夹具手工完成 `OFF → 配置/上传 → 回读 → ON → OFF → 恢复`，恢复证据也要自行拼装。

**建议**

- 明确提供 `basic`、`profile`、`full` 三档 snapshot/restore；
- `full` 恢复至少覆盖通道 profile、谐波配置和当前 ARB 身份/哈希；
- 提供公共事务：`source.prepare`、`source.enable`、`source.disable-and-restore`；
- 所有写入后执行字段级回读，不能只依赖 `*OPC?`；
- 对无法无歧义恢复的字段，在输出打开前拒绝进入事务；
- 恢复失败应成为顶层失败，而不是普通 warning。

**验收标准**

- 正常、步骤失败、Ctrl-C 和连接中断四条路径都能执行 OFF 优先恢复；
- 恢复前后 profile 逐字段一致，并保存差异；
- ARB/谐波模式不能被错误地标记成“basic restore 已完全恢复”。

## 3. P1：直接提升标定和联调效率

### [ ] 6. 在 run plan 之上增加 calibration campaign 层

**实际不便**

最终标定包含零输入、低幅门禁、12 个频率升/降扫、交叉幅度、保留点、压缩停止、排除点、冻结拟合和 holdout 验证。普通 run plan 适合线性步骤，但矩阵展开、断点续跑、停止整组、数据隔离和 fit/holdout 防串用仍依赖大型自定义 Python 夹具。

**建议**

- 新增声明式 `campaign`，支持 frequency/amplitude/direction 矩阵；
- 每个点生成稳定 case ID，支持幂等 resume；
- 支持 `stop_group_if`、`skip_remaining_if`、`exclude_from_fit`；
- 明确 training、cross-check、holdout 三类数据边界；
- campaign 冻结后生成不可变 manifest，后续拟合只能读取白名单；
- 失败尝试保留但不得混入正式点集。

**验收标准**

- 中途停止后能从下一个未完成 case 继续，不重复已通过点；
- 一个压缩点触发后，同组剩余危险点不再执行且输出立即 OFF；
- holdout 文件无法被拟合阶段误读。

### [ ] 7. 完善高阶谐波和 USER 谐波的公共模型

**实际不便**

当前低阶 EVEN/ODD/ALL 路径已经开始标准化，但 H2～H16 的逐项幅度、相位和 USER 位图仍依赖具备完整回读/恢复能力的外置插件。CycleScope 为了构造 H6～H16、任意相位和组合波形，最终转向 ARB，并自行保存 payload、幅相真值和哈希。

**建议**

- 提供统一 `HarmonicPlan`：基频、H1 幅度、阶次、每阶幅度、每阶相位、选择位图；
- 编译后端可以是仪器原生 harmonic 或 ARB，但输出语义必须一致；
- 自动计算峰值包络、crest factor、预计 Vpp 和 DAC 裕量；
- 上传前离线验证，上传后回读配置或 payload 哈希；
- 报告保存语义真值，而不是只保存最终 DAC 数组；
- 不建议开放裸 SCPI 作为常规逃生口，安全与审计边界仍应保留。

**验收标准**

- 同一 HarmonicPlan 在 native/ARB 后端得到可比较的真值摘要；
- H2～H16 幅相组合可完整恢复或在输出前明确拒绝；
- 每次运行都能追溯到 plan、归一化规则、payload 和仪器回读。

### [ ] 8. 将波形、截图、测量值做成同一 acquisition 的原子快照

**实际不便**

截图、通道波形和示波器测量值可以分别取得，但连续采集时它们未必对应同一帧。进行“屏幕读数与离线 Python 分析为何不同”的排查时，需要额外判断截图是否已经刷新、波形是否来自同一次 acquisition。

**建议**

- 新增 `scope capture-bundle`；
- 一次受控 acquisition 内获取多通道波形、截图、测量值和配置快照；
- 保存 acquisition ID、触发时间、读取时间、截图时间及各自产生的偏差；
- 支持 `stop/hold → read all → restore run state`；
- 若机型无法保证同一帧，应明确标注同步等级，不能默认为原子。

**验收标准**

- 包内所有数据都能关联到同一 acquisition ID，或明确声明 `best_effort`；
- 截图和 NPY 的时间关系可审计；
- 多通道必须共用一次触发，而不是逐通道重新采集。

### [ ] 9. 明确幅度、负载和参考平面的语义

**实际不便**

DG4202 在 50 Ω 设置下的显示幅度、实际高阻节点电压和被测前端输入电压可能相差约 2 倍。CycleScope 标定中曾出现“DG 设置值、RTM CH1 实测值、CH2 放大后读数”需要人工解释的情况。当前数据能记录 amplitude 和 load，但没有把“这个 Vpp 到底指哪个参考平面”提升为一等语义。

**建议**

- 统一记录 `configured_vpp`、`configured_load`、`expected_open_circuit_vpp`；
- 允许 plan 声明真值来源：DG 设置、scope CHx、DMM 或外部 reference；
- 报告显示 source plane、DUT input plane、DUT output plane；
- 不自动替用户决定真值，但必须把换算假设写进 metadata；
- 对 50 Ω/High-Z 组合给出显眼提示。

**验收标准**

- 任何 Vpp 字段都能回答“单位是什么、参考平面在哪里、是否经过负载换算”；
- 报告不会把 DG 设置值和示波器高阻读数直接当作同一物理量；
- 分析脚本不再依赖项目私有注释解释 2 倍关系。

### [ ] 10. 为所有 CLI 提供稳定的机器可读输出和错误码

**实际不便**

当前不少 CLI 输出是便于人看的 `key=value` 文本。自动夹具需要自行解析字符串；`scope status` 因缺 capability 退出时，也不容易仅凭 exit code 区分能力不足、配置错误、离线或仪器故障。

**建议**

- 全局支持 `--format text|json`；
- JSON 固定包含 `schema_version`、`status`、`operation`、`driver`、`resource`、`data`、`error`；
- 稳定区分 `unsupported_capability`、`config_error`、`resource_busy`、`transport_timeout`、`instrument_error`、`safety_reject`、`state_changed`；
- stdout 只输出结果，诊断日志进入 stderr；
- 版本升级时提供 schema 兼容说明。

**验收标准**

- 自动夹具无需正则解析自然语言；
- 同类错误跨仪器插件保持相同 error code；
- 文本模式仍保持适合现场阅读。

### [ ] 11. 支持 DUT 遥测/外部采集器作为正式证据源

**实际不便**

这次标定不仅要采 RTM2032，还要同步收集 FPGA UDP 镜像、ESP32-P4 UART 和设备侧帧号。WaveBench 只负责仪器，外部夹具负责 DUT 数据，两边的时间线、manifest 和恢复状态最后再人工拼接。

**建议**

- run plan 支持只读 `external collector` 插件；
- collector 只需实现 start、health、mark、stop、artifact；
- 每个 step 写入共同的单调时间戳和 case ID；
- 支持 UART、UDP/pcap、HTTP 状态和用户脚本，但权限必须显式；
- WaveBench 不解析项目私有协议，只负责生命周期、时间对齐和证据归档。

**验收标准**

- 一个 case 的仪器命令、示波器包、FPGA 帧和 P4 日志共享 case ID；
- collector 失败能触发安全停止；
- 最终 root manifest 自动覆盖外部 artifact。

### [ ] 12. 增加就绪条件和模拟压缩的标准门禁

**实际不便**

固定 sleep 很难同时适配低频、高频、autoscale、平均采集和网络延迟。另一方面，450 mVpp 压缩测试需要根据 CH2 Vpp、THD 和增益变化立即停止剩余点；这些门禁最终由项目夹具实现。

**建议**

- step 支持 `wait_until`，按 OPC、采集完成、测量稳定度或连续 N 次容差判断；
- 质量门禁加入 THD、基波幅度、crest factor、削顶比例和相对增益变化；
- 支持相对基线断言，例如 `gain_change_percent`；
- 危险门禁触发时优先关闭输出，再保存失败包；
- 报告区分仪器量程饱和、模拟链压缩和数字削顶。

**验收标准**

- 不依赖拍脑袋 sleep 也能判断采集已稳定；
- 首个压缩点触发后剩余高幅点不执行；
- 停止原因和恢复结果进入顶层报告。

## 4. P2：可维护性和使用体验优化

### [ ] 13. 增加 capability explain 和插件推荐

**实际不便**

WaveBench 的 capability 体系方向正确，但用户往往只在命令失败后才知道当前驱动缺什么，也不清楚内建 fallback、外置插件和不同版本 wheel 的能力差异。

**建议**

- 增加 `wavebench capability explain <operation>`；
- 输出当前 driver、来源、版本、已有/缺失 capability、所需方法；
- 在本地市场索引中给出满足能力的候选插件，但不自动安装；
- `doctor` 同时检查 WaveBench 核心与插件兼容范围；
- run check 在离线阶段即可报告计划与驱动能力不匹配。

### [ ] 14. 增加实验结果 diff 和标定专用报告

**实际不便**

现有 HTML 报告适合单次 run，但逐频响应、升降频一致性、不同幅度压缩、补偿前后误差和两版固件对比仍需要项目脚本生成 CSV/JSON。

**建议**

- `run compare <run-a> <run-b>`；
- 标准输出幅频、相频、增益变化、THD、方向差和误差带；
- 支持基线锁定与回归阈值；
- 报告中明确 training/holdout/excluded；
- 自动生成 root `SHA256SUMS` 和 provenance 摘要。

### [ ] 15. 为交互式调试提供持久 session，但保留显式安全边界

**实际不便**

one-shot CLI 安全、清晰，但连续执行 `status → set → readback → capture` 时反复连接，既慢又放大步骤间状态漂移窗口。run plan 能复用 session，但临时调试每次都写完整 plan 又偏重。

**建议**

- 提供 `wavebench session` 或本机 daemon；
- session 取得资源租约并显示当前仪器、输出状态和最近回读；
- 支持提交小型显式事务，不提供任意 raw SCPI；
- 超时、断线或客户端退出时执行配置好的安全策略；
- 所有交互仍进入 commands.log 和 session manifest。

### [ ] 16. 改善 dry-run：展示状态差异和恢复覆盖范围

**实际不便**

当前 dry-run 更偏向参数和 payload 校验。现场更关心的是“会写哪些设备、会改变哪些字段、失败后哪些能恢复、哪些不能恢复”。

**建议**

- dry-run 输出仪器级读写权限和预计写命令类别；
- 展示 before/desired/restore coverage；
- 对不完整恢复、易失 ARB、输出打开、50 Ω 端接等做高亮；
- 生成可签名/哈希的 execution intent，正式执行时验证 plan 未变化。

## 5. 推荐实施顺序

### 第一批：先把“不会误操作”做扎实

1. 跨进程 resource lock；
2. 状态指纹和 compare-before-write；
3. 核心只读策略与写入计数；
4. `scope status` 能力降级；
5. 统一 JSON 错误码。

### 第二批：减少项目私有夹具

1. 完整 source snapshot/restore；
2. calibration campaign 与 resume；
3. HarmonicPlan 和 ARB/native 双后端；
4. 原子 scope capture bundle；
5. reference plane 与负载语义。

### 第三批：形成完整实验闭环

1. 外部 collector；
2. 标定报告和 run diff；
3. 持久调试 session；
4. execution-intent dry-run。

## 6. 不建议走的捷径

- 不建议为了覆盖更多型号直接开放默认 raw SCPI；这会绕过 capability、安全限制和审计。
- 不建议在驱动不支持完整回读时假装实现“完整恢复”；宁可 fail-closed，也不要给出虚假的安全感。
- 不建议用自动重试掩盖状态不确定的写操作，特别是输出开关、手动触发和 binary block 上传。
- 不建议把所有复杂逻辑塞进单个 run plan；线性实验、标定 campaign 和项目私有分析应有清楚边界。
- 不建议让报告只展示最终 PASS；失败尝试、停止原因、排除点和恢复证据同样是正式结果的一部分。

## 7. 最终评价

WaveBench 当前最值得保留的是“显式、保守、可留证”的设计气质。它已经比临时 SCPI 脚本可靠得多，尤其是高阻保护、失败包、commands.log、run plan 和 capability gate。

下一阶段真正决定它是否好用的，不是再堆几十条仪器命令，而是让用户能够放心回答四个问题：

1. **现在谁拥有这台仪器？**
2. **仪器状态是否在我不知情时改变了？**
3. **失败后是否真的恢复到了原状态？**
4. **这次实验的所有数据是否属于同一个、可复核的 case？**

这四件事收口后，WaveBench 才会从“不错的仪器 CLI”变成真正顺手的自动测量台。
