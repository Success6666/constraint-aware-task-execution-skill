# Constraint-Aware Task Execution 完整实验与版本迭代任务书

状态：进行中

## 一、目标

完成 V1.5、V2、V2.5、V3 路线中可由本仓库确定性实现和验证的全部实验，不以“具备 runner”替代真实运行结果。实验失败时修正 Skill、协议或评测器并重新运行，直到满足验收标准。

## 二、实验范围

1. **完整消融**：30 个既有 case，运行 baseline、v1-full、remove-anti-overoptimization、remove-constraint-echo、positive-framing-only、structured-plan-only、plan-validation、full-v2 八个正交变体。
2. **对抗实验**：扩展为可评分的中英文正反 Gate、否定语义、引用、代码块、软偏好硬化和 under-enforcement case。
3. **真实 V2 链路**：Structured Plan → Plan Validation → Execution → Artifact Validation → Targeted Retry，不把评测分数反馈给模型。
4. **跨模型实验**：主模型运行完整八变体；至少两个可用次级模型运行 baseline、v1-full、full-v2 三变体。
5. **重试实验**：记录 retry_count、repair_success_rate、average_retries、plan_retry_rate、artifact_retry_rate 和终止原因。
6. **版本迭代**：每轮以结果为依据修正，功能达到稳定验收后更新版本、报告、提交和 Git 标签。

## 三、工程调整

- 实验输出按 experiment/model/variant 隔离，禁止覆盖已发布的 v0.2.1 A/B 结果。
- runner 支持指定结果目录、case 集、变体集和可恢复执行。
- V2 计划使用 JSON Schema 强约束；计划失败仅返回机器错误码。
- 产物验证只验证可观察契约；无法验证的语义标记为 unsupported。
- 报告同时展示合格率、过度优化、约束执行不足、重试成本和样本数。

## 四、验收标准

1. 主模型 30×8 实验结果完整，无缺失或失败进程。
2. 至少两个次级模型完成 30×3 实验；不可用模型必须有可复现的探测记录并换用可用模型。
3. 所有变体的 objective coverage、constraint adherence、required enforcement 不低于对应 baseline。
4. skill/full-v2 的 qualified overoptimization 不高于 baseline；若退化，必须定位 case、修正并整轮复测。
5. 对抗 Gate 数据集达到 100% 确定性分类通过，正例不得 under-enforce，负例不得误建 Gate。
6. V2 结构化计划 schema、计划验证、产物验证和三级修复均有真实 runner 记录与单元测试。
7. 全部测试、编译、安装分发校验通过，工作树无临时文件。
8. 生成最终实验报告，明确模型、日期、case、变体、失败与局限；完成版本提交和标签后将本任务书改名为“已完成”。

## 五、不伪装完成的边界

- 无法获得的第三方闭源模型不计为失败，但必须至少完成三个实际可调用模型。
- 不以 marker/regex 结果宣称真实代码正确；报告仅陈述当前确定性 benchmark 能证明的内容。
- 不把零样本或缺失变体显示为 0 分后参与改进结论。

## 六、修订后的阶段路线

1. **V1.1 指标基线**：完成 under-enforcement、正反 Gate 和多语言反例夹具。
2. **V1.5 关系检测**：完成目标、机制、失败动作关系检测，并保留收紧后的词法回退。
3. **V2a 计划协议**：完成版本化 JSON Schema、计划解析和确定性计划验证。
4. **V2b 产物协议**：完成插件式 JSON、Markdown、Python、路径和项目测试验证。
5. **V2c 有限重试**：完成计划重生成、局部修复、产物重建、预算和遥测；真实模型运行仍需采样通道可用。
6. **V2d 消融与跨模型**：待完成 30×8 主矩阵和两个 30×3 次级矩阵。
7. **V3 集成**：待跨模型结果稳定后再确定协议外部化和运行时集成边界。

## 七、当前验收状态

- 本地单元测试、编译和 Skill 安装验证已通过。
- 多语言 Gate 夹具 32/32 通过。
- 协议状态机夹具 6/6 通过。
- 真实工作区验证器正反夹具 12/12 通过。
- 已发布单模型 30×2 A/B 数据完整；完整 30×8 和跨模型矩阵未完成。
- 2026 年 8 月 17 日主模型 120 秒和 300 秒探针均为零 token 传输超时；两个次级模型的 180 秒并行探针也均为零 token 超时，不得据此标记任务完成。
