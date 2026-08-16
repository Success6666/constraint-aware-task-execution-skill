# Constraint-Aware-Task-Execution V1.5/V2 迭代任务书

状态：已完成

## 一、原始问题

当前仓库已完成 V1 Skill、30 条中英文 A/B benchmark、确定性 scorer 和跨 Agent 安装验证，但仍以“模型直接输出设计文本”为主，无法验证结构化计划、实际产物或定向修复。附带计划书提出的 V2 方向正确，但需要收敛为当前仓库可验收的协议与评测能力。

## 二、方案修订

1. 保留 Objective First、硬约束/软偏好区分、最小满足、禁止无请求门禁、比例原则。
2. 将“模型不得生成无请求门禁”与“运行时确定性验证”分离：后者仅用于显式输出格式、文件范围、安全 enforcement 或测试契约，不作为普通约束的默认 gate。
3. Gate 检测升级为“约束目标 + enforcement 机制 + 失败动作”的关系检测，保留关键词 fallback，并补充否定、引用、代码块和中文反例。
4. V2 先实现可版本化的结构化执行计划、确定性计划验证、插件式产物验证和有限重试状态机；对无法证明语义完成的任务返回 `unsupported` 或 `partial`，不伪造通过。
5. 消融 runner 支持 baseline、V1 full、positive framing、structured plan、plan validation、full V2 六类变体；不把 overoptimization 分数反馈给模型。
6. V2.5/V3 的多模型、开放 benchmark 和真实 Agent Runtime 集成不作为本轮完成标准，仅保留后续接口。

## 三、交付范围

### 阶段 1：V1.5 scorer 与反例

- 新增 under-enforcement 指标。
- 新增显式要求建立 gate 与普通禁止约束的成对 case。
- 结构关系 gate 检测和反误报测试。

### 阶段 2：Structured Plan

- 新增 `evals/protocol.py`，提供 JSON 计划数据结构、schema 校验、计划验证结果。
- 计划字段至少包含 objective、hard_constraints、soft_preferences、risk_points、artifacts、validation_profile。
- 明确 `constraint`、`implementation_strategy`、`failure_gate` 三者不能混淆。

### 阶段 3：Artifact Validator

- 插件式验证 JSON、Markdown、文件路径范围、Python AST/编译。
- 通用任务对不支持的语义检查返回 `unsupported`，不当作 PASS。

### 阶段 4：Targeted Retry

- 实现局部修复、当前产物重建、重新规划三级状态机。
- 具备最大重试次数、错误上下文、幂等记录和 retry 指标。

### 阶段 5：消融与文档

- 扩展 `run_ab.py` 的变体与签名协议。
- 生成确定性消融汇总脚本和报告模板。
- README、Skill 文档、测试与路线图同步。

## 四、验收标准

1. `python -m unittest discover -s tests -v` 全部通过，且覆盖新增协议、验证器、重试、scorer 反例和消融配置。
2. 结构化计划能拒绝空 objective、缺 strategy、普通约束被错误声明为 failure gate 的输入。
3. 显式安全/格式/路径契约可被验证；不支持的语义验证明确返回 `unsupported`。
4. gate 检测对普通否定、引用说明、代码块示例不误判，并能识别高置信度三元关系。
5. 重试状态机根据错误范围选择 Level 1/2，仅计划结构错误进入 Level 3，达到上限后停止。
6. 现有 30-case 结果文件和 V1 兼容测试不被破坏；README 不宣称跨模型普遍有效。
7. 功能变更完成后按版本规则提交，提交说明包含完整功能变更；任务书完成后将文件名追加“已完成”。

## 五、非本轮验收项

- 真实闭源模型跨模型效果结论。
- 需要人工或其他 AI 评分的语义质量判断。
- V3 Agent Runtime 的实际执行集成。
