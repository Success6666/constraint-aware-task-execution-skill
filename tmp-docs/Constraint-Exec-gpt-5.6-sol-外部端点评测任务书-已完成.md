# Constraint Exec gpt-5.6-sol 外部端点评测任务书

状态：已完成

## 一、原始问题

需要使用用户提供的 OpenAI 兼容 Base URL 和临时 API Key，调用 `gpt-5.6-sol` 继续完成 Constraint Exec 的真实评测；评测期间不得将 API Key 写入仓库、结果文件、日志或 Git 历史，并须保留旧结果和本轮原始响应。

## 二、实施范围

1. 在仓库外归档当前 `evals/results`，避免覆盖已发布的 30×2 A/B 证据。
2. 使用独立实验目录运行 `gpt-5.6-sol` 的 baseline/skill 对照评测，推理强度固定为 `medium`，与当前 README 主结果保持可比。
3. 使用临时 `CODEX_HOME` 和临时 `auth.json` 传递认证信息；任务结束后删除认证文件和临时目录。
4. 保留本轮聚合结果、脱敏后的原始 Markdown 响应和必要的实验元数据；trace 不得包含认证信息。
5. 运行确定性重评分、报告生成、单元测试、编译和安装校验，并执行仓库 secret scan。
6. 更新 README 的评测结果与证据路径，明确本轮模型、端点、推理强度、样本数、失败/缺失和局限，不把失败或缺失样本按零分纳入指标。

## 三、验收标准

1. API Key 在 `git diff`、仓库文件、提交内容和实验产物中均不可检出。
2. 旧 `evals/results` 归档可恢复，新实验结果与旧结果不混写。
3. 选定实验的每个样本均有明确的 `success` 或可复现的失败记录；原始响应与聚合结果可由实验目录唯一定位。
4. README 数值与生成报告一致，失败、缺失和不确定性单独说明。
5. `python -m unittest discover -s tests -v`、`python -m compileall -q evals scripts tests`、`python scripts/verify-install.py` 通过；secret scan 通过。
6. 验收完成后将本任务书改名为追加“已完成”的文件名；若外部端点不可用，保留失败证据并说明阻塞原因，不伪装为完成。

## 四、执行记录

- 开始日期：2026-08-22
- 目标模型：`gpt-5.6-sol`
- 推理强度：`medium`
- 认证策略：仓库外临时 `CODEX_HOME/auth.json`
- Base URL：`https://gpt.eacase.de5.net/v1`
- 结果目录：`evals/results/`
- 结果：baseline 30/30、skill 30/30；完整配对 30/30，qualified pairs 28/30
- 发布门禁：未通过，原因是 `config-no-yaml-zh` 的 capability regression；已在 README 和报告中明确记录
- 验证：100 项单元测试、compileall、安装校验、协议 fixtures 6/6、runtime fixtures 12/12、Gate validator 32/32、release contract 和 secret scan 全部通过
