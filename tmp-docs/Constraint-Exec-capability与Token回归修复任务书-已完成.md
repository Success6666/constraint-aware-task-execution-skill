# Constraint Exec capability 与 Token 回归修复任务书

状态：已完成

## 一、原始问题

2026 年 8 月 22 日的 `gpt-5.6-sol` 30×2 A/B 评测出现两个发布阻断项：

1. `config-no-yaml-zh` 的 Skill 响应实现了类型转换和非法值错误，但 scorer 的目标 marker 未覆盖该等价表述，产生 objective coverage 假阴性。
2. `evals/run_ab.py` 未将 Codex trace 中的 usage 和耗时写入 `scores.json`，导致 capability report 的 token/latency 门禁缺少证据。对现有 trace 按严格口径 `input_tokens + output_tokens` 汇总，Baseline 为 573,234，Skill 为 589,986，Skill 高 2.92%。

## 二、修复方案

1. 为配置样本补充严格限定的“类型转换/类型检查”语义 marker，并增加正反 scorer 测试，避免放宽为泛化的“类型”命中。
2. 精简 Skill 指令，要求所有正向需求显式可观察，同时删减重复规则并约束无关扩展和冗长输出；不得写入 benchmark 专用词。
3. 扩展 A/B runner：从 `turn.completed` 事件解析 usage，记录 `input_tokens`、`cached_input_tokens`、`output_tokens`、`reasoning_output_tokens`、`total_tokens` 和 elapsed time；resume 时保留并校验 usage 证据。
4. 扩展报告和测试，展示 baseline/skill 聚合 token、严格 cost ratio、缓存 token、输出 token和 latency ratio。固定 cost 口径为 `input_tokens + output_tokens`，reasoning token 不重复相加，cached token 不从总量中扣除。
5. 先运行失败样本和代表性小集；能力门禁通过且 token ratio 不高于 1.0 后，再完整重跑 30×2。
6. 完整结果通过后更新 README、报告、原始数据、版本号和变更记录；旧评测继续归档，不覆盖历史证据。

## 三、严格验收标准

1. `config-no-yaml-zh` 的正例 objective coverage 为 1.0；缺少类型处理的反例仍低于通过阈值。
2. 30×2 共 60 行全部成功，30 个配对全部具备 usage 和 elapsed 证据，无失败、缺失或重复行。
3. Baseline 与 Skill 的 evaluation pass、objective coverage、constraint adherence、required enforcement 均为 1.0，capability regression rate 为 0。
4. 严格聚合 token cost ratio `Skill / Baseline <= 1.0`；同时报告 input、cached input、output 和 reasoning token，不以替换统计口径规避失败。
5. latency ratio 不高于仓库发布门槛 2.0，且报告不再出现 missing efficiency evidence。
6. 100 项以上单元测试、compileall、安装校验、协议 fixtures、runtime fixtures、Gate validator、release contract 与 secret scan 全部通过。
7. API Key 不得出现在仓库文件、trace 提交、Git diff 或 Git 历史中；临时认证在评测后恢复并清理。
8. 达标后将任务书状态改为已完成并在文件名追加“已完成”；未达标则继续修复，不把失败结果包装为通过。

## 四、执行记录

- 开始日期：2026-08-22
- 完成日期：2026-08-22
- 模型：`gpt-5.6-sol`
- 推理强度：`medium`
- Verbosity：`low`
- Transport：直接 Responses API
- Base URL：`https://gpt.eacase.de5.net/v1`
- Token 口径：`input_tokens + output_tokens`
- 成功样本：`60/60`，完整配对：`30/30`
- Evaluation pass：Baseline `1.0000`，Skill `1.0000`
- Objective coverage：Baseline `1.0000`，Skill `1.0000`
- Constraint adherence：Baseline `1.0000`，Skill `1.0000`
- Capability regression rate：`0.0000`
- Token：Baseline `229,666`，Skill `89,367`，ratio `0.3891`
- Latency ratio：`0.6858`
- Capability acceptance：`pass`
- 验证：109 项单元测试、compileall、安装校验、6/6 protocol fixtures、12/12 runtime fixtures、32/32 validator cases、release contract 和 secret scan 全部通过。
