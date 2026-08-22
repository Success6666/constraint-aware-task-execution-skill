# 命令行日志分析器设计

建议使用 Python 3.11+ 实现，仅依赖标准库：`argparse`、`datetime`、`json`、`re`、`sys`、`collections`、`dataclasses`、`unittest`。

工具暂名：`log-analyzer`

## 1. 功能范围

支持：

- 从文件或标准输入流式读取日志
- 按时间范围过滤
- 统计日志级别
- 聚合错误信息
- JSON 或人类可读格式输出
- 处理超大日志文件，不将全部内容加载到内存
- 对非法行、缺失字段和未知级别进行稳定处理
- 单元测试、集成测试和命令行测试

默认只维护聚合结果，内存复杂度为：

```text
O(错误类型数量 + 统计维度数量)
```

而不是：

```text
O(日志总行数)
```

## 2. 推荐日志格式

首期支持一种明确格式：

```text
2025-01-15T10:30:12Z ERROR database connection failed
2025-01-15T10:30:13Z INFO request completed
2025-01-15T10:30:14+08:00 WARN retrying request
```

格式定义：

```text
<TIMESTAMP> <LEVEL> <MESSAGE>
```

其中：

- `TIMESTAMP`：ISO 8601 时间
- `LEVEL`：`DEBUG`、`INFO`、`WARN`、`WARNING`、`ERROR`、`FATAL`、`CRITICAL`
- `MESSAGE`：剩余文本，可为空

推荐解析正则：

```regex
^(?P<timestamp>\S+)\s+(?P<level>[A-Za-z]+)(?:\s+(?P<message>.*))?$
```

后续可以扩展 Apache、Nginx 或 JSON Lines 格式，但不应在第一版中隐式猜测多种格式，否则错误诊断和边界行为会不明确。

## 3. 命令行接口

### 基本用法

```bash
log-analyzer app.log
```

从标准输入读取：

```bash
cat app.log | log-analyzer -
```

指定多个文件：

```bash
log-analyzer app-*.log
```

建议由程序显式处理 glob，或由 shell 展开后接收多个路径。

### 时间过滤

```bash
log-analyzer app.log \
  --from 2025-01-15T00:00:00Z \
  --to 2025-01-16T00:00:00Z
```

时间范围采用半开区间：

```text
[from, to)
```

也就是：

- `timestamp >= from` 才保留
- `timestamp < to` 才保留

这样可以自然拼接相邻时间窗口，避免边界重复统计。

如果日志时间带时区，统一转换为 UTC 后比较。无时区输入默认拒绝，并返回参数错误；也可以提供：

```bash
--timezone UTC
```

但第一版建议只接受带时区的时间戳，减少歧义。

### 输出格式

人类可读格式：

```bash
log-analyzer app.log --output text
```

JSON 格式：

```bash
log-analyzer app.log --output json
```

默认输出 `text`，脚本和 CI 场景使用 `json`。

### 错误聚合粒度

默认按完整错误消息聚合：

```bash
log-analyzer app.log --level ERROR
```

可选按规范化后的错误消息聚合：

```bash
log-analyzer app.log --normalize-errors
```

规范化规则建议仅处理高确定性的动态内容：

- 数字替换为 `<num>`
- UUID 替换为 `<uuid>`
- 十六进制长标识替换为 `<hex>`
- IP 地址替换为 `<ip>`
- 多余空白合并

例如：

```text
user 123 failed to connect to 10.0.0.1
user 456 failed to connect to 10.0.0.2
```

聚合为：

```text
user <num> failed to connect to <ip>
```

不要默认删除任意数字或任意字符串，避免把本来不同的错误错误地合并。

### 排序和数量限制

```bash
log-analyzer app.log --top 20
```

含义是只在错误聚合结果中输出出现次数最高的 20 项。

建议支持：

```bash
--sort count
--sort first-seen
```

默认：

```text
count 降序，相同次数按首次出现顺序
```

如果只需要统计错误总数而不需要所有错误消息，可支持：

```bash
--no-error-details
```

这样可以进一步降低内存占用。

### 非法行处理

默认跳过非法行，并在汇总中记录：

```bash
log-analyzer app.log --strict
```

严格模式下，遇到第一条非法行立即退出，返回非零状态码。

建议支持：

```bash
--max-invalid 100
```

超过上限后退出，防止输入格式错误导致无意义地继续处理。

## 4. 内部数据模型

使用 `dataclasses`：

```python
@dataclass(frozen=True)
class LogRecord:
    timestamp: datetime
    level: str
    message: str
    source: str
    line_number: int
```

解析结果不必长期保存。读取一行后：

1. 解析为 `LogRecord`
2. 判断时间范围
3. 更新统计
4. 丢弃记录

聚合状态：

```python
@dataclass
class AnalysisResult:
    total_lines: int
    parsed_lines: int
    matched_lines: int
    invalid_lines: int
    skipped_lines: int
    level_counts: Counter[str]
    error_counts: Counter[str]
    first_timestamp: datetime | None
    last_timestamp: datetime | None
```

其中：

- `total_lines`：实际读取的总行数
- `parsed_lines`：成功解析的行数
- `matched_lines`：解析成功且在时间范围内的行数
- `invalid_lines`：解析失败的行数
- `skipped_lines`：解析成功但被时间范围过滤的行数
- `level_counts`：范围内各级别统计
- `error_counts`：范围内错误级别消息统计
- `first_timestamp`、`last_timestamp`：范围内首尾日志时间

`WARN` 和 `WARNING` 建议统一为 `WARN`，`FATAL` 和 `CRITICAL` 可以统一为 `FATAL`，但应在文档中明确。

规范化级别：

```python
LEVEL_ALIASES = {
    "WARNING": "WARN",
    "CRITICAL": "FATAL",
}
```

未知级别不要直接视为错误。可以归一化为大写并计入 `UNKNOWN`，或者将其作为非法行。推荐：

- 结构合法但级别未知：`UNKNOWN`
- 时间戳或整体结构非法：非法行

## 5. 模块职责

建议分成以下逻辑层，避免 CLI 参数解析、日志解析和输出格式相互耦合。

### `parse_timestamp(value)`

职责：

- 解析 ISO 8601 时间
- 支持 `Z`
- 支持显式时区偏移
- 返回带时区的 `datetime`
- 将时间转换为 UTC

错误时抛出专用异常，例如：

```python
class InvalidTimestamp(ValueError):
    pass
```

Python 的 `datetime.fromisoformat()` 可以作为基础解析器。需要额外把结尾 `Z` 转换为 `+00:00`，以兼容不同 Python 版本。

### `parse_line(line, source, line_number)`

职责：

- 去除行尾换行符
- 解析时间、级别、消息
- 标准化级别
- 返回 `LogRecord`

不要在该函数中处理时间范围或统计逻辑，使其可以独立测试。

### `iter_lines(paths)`

职责：

- 逐个打开输入文件
- `-` 表示标准输入
- 以文本模式读取
- 使用 `utf-8`，建议 `errors="replace"`，避免单个坏字节终止整个分析
- 产出 `(source, line_number, line)`

文件资源必须使用上下文管理器。标准输入不应被关闭。

### `analyze(records, options)`

职责：

- 接收迭代器
- 更新 `AnalysisResult`
- 应用时间过滤
- 统计级别与错误
- 处理非法行策略

该函数不直接打印内容。

### `render_text(result, options)`

输出示例：

```text
Log analysis
------------
Total lines:    125000
Parsed lines:   124980
Matched lines:  80000
Invalid lines:  20
Filtered lines: 44980

Time range:
  first: 2025-01-15T10:00:00Z
  last:  2025-01-15T10:59:59Z

Levels:
  INFO:  70000
  WARN:  8000
  ERROR:  2000

Top errors:
  120  database connection failed
   87  request timeout
```

不要将日志原文直接写入标准错误。标准错误只用于诊断信息，例如严格模式下的非法行位置。

### `render_json(result, options)`

使用 `json.dump()` 输出，确保：

- 日期统一使用 UTC ISO 8601
- 计数值为整数
- 空集合输出 `{}` 或 `[]`，保持结构稳定
- 不输出不可序列化的 `datetime` 对象

## 6. JSON 输出协议

建议固定为以下结构：

```json
{
  "summary": {
    "total_lines": 125000,
    "parsed_lines": 124980,
    "matched_lines": 80000,
    "invalid_lines": 20,
    "filtered_lines": 44980,
    "first_timestamp": "2025-01-15T10:00:00Z",
    "last_timestamp": "2025-01-15T10:59:59Z"
  },
  "levels": {
    "INFO": 70000,
    "WARN": 8000,
    "ERROR": 2000
  },
  "errors": [
    {
      "message": "database connection failed",
      "count": 120,
      "first_seen": "2025-01-15T10:01:00Z",
      "last_seen": "2025-01-15T10:59:00Z"
    }
  ],
  "options": {
    "from": "2025-01-15T10:00:00Z",
    "to": "2025-01-15T11:00:00Z",
    "normalized_errors": false
  }
}
```

错误聚合项建议额外维护：

```python
@dataclass
class ErrorAggregate:
    count: int
    first_seen: datetime
    last_seen: datetime
    first_index: int
```

最终按：

1. `count` 降序
2. `first_index` 升序

排序后输出。

JSON 输出必须只写到标准输出，日志诊断写到标准错误，保证：

```bash
log-analyzer app.log --output json | jq '.levels.ERROR'
```

可以稳定工作。

## 7. 时间范围行为

推荐规则：

- 只提供 `--from`：保留不早于起始时间的日志
- 只提供 `--to`：保留早于结束时间的日志
- 同时提供：使用 `[from, to)`
- `from >= to`：参数错误，退出码 `2`
- 时间戳格式错误：参数错误，退出码 `2`
- 日志中的非法时间戳：按非法行处理
- 日志中存在不同时间区：全部转换为 UTC 后比较

不应假设日志按时间排序。这样工具既适合实时流，也适合乱序归档文件。

## 8. 流式读取与性能要求

实现要求：

- 使用 `for line in file`
- 不使用 `read()`、`readlines()` 或一次性 `list(file)`
- 不保存原始日志行
- 默认不保存所有非错误记录
- 仅保存错误聚合键和统计数据
- 输出 JSON 时才构造最终可序列化结构

大文件性能目标：

- 时间复杂度：`O(n)`
- 每行只解析一次
- 错误聚合平均为 `O(1)`
- 内存不随日志行数线性增长

需要明确一个边界：如果错误消息种类非常多，`error_counts` 仍可能增长。可通过以下方式控制：

```bash
--top 100
```

但仅在分析结束时限制输出并不能限制内存。若需要严格内存上限，应提供：

```bash
--max-error-groups 10000
```

达到上限后：

- 继续统计总错误数和级别数
- 停止记录新的错误消息
- 在 JSON 中增加：

```json
"error_groups_truncated": true
```

第一版可以先实现完整聚合，再预留该选项。

## 9. 退出码

建议定义：

```text
0  分析成功
1  运行时错误，例如文件无法读取
2  命令行参数或输入格式错误
3  严格模式下发现非法日志行
```

非严格模式下，存在非法行仍返回 `0`，但通过 `invalid_lines` 和标准错误提示暴露问题。

## 10. 测试方案

使用标准库 `unittest`，不引入 pytest。

### 解析单元测试

覆盖：

- 标准 UTC 时间戳
- 带时区偏移的时间戳
- 结尾 `Z`
- 无消息的日志
- 消息中包含空格
- 小写级别
- `WARNING`、`CRITICAL` 别名
- 空行
- 缺少时间戳
- 缺少级别
- 非法时间戳
- UTF-8 替换字符

### 时间过滤测试

覆盖：

- 无过滤条件
- 只有 `from`
- 只有 `to`
- 刚好等于 `from`
- 刚好等于 `to`
- `from == to`
- `from > to`
- 不同时间区转换后处于范围内
- 日志乱序

特别验证半开区间：

```text
from 时间应被包含
to 时间应被排除
```

### 聚合测试

覆盖：

- 多个错误消息计数
- 相同错误按规范化前后分别聚合
- 错误消息首次和末次时间
- 相同计数时按首次出现顺序
- `top N`
- 没有错误时输出空集合
- 未知级别不会进入错误统计

### 流式行为测试

使用自定义迭代器验证分析器只消费迭代器，不要求可索引或可重复遍历。

可以增加一个会在第二次迭代时失败的迭代器，确保实现没有隐式重新读取数据。

### 文件与标准输入测试

覆盖：

- 单文件输入
- 多文件输入
- `-` 读取标准输入
- 不存在的文件
- 文件编码异常
- 每个文件的行号在错误提示中正确显示

### JSON 测试

验证：

- 输出可被 `json.loads()` 解析
- 所有计数为数字
- 时间字段为字符串或 `null`
- 空输入仍有完整结构
- JSON 输出不混入诊断文本
- `top N` 后结果顺序稳定

### CLI 集成测试

使用 `subprocess` 调用命令行入口，检查：

- 默认文本输出
- `--output json`
- `--from`、`--to`
- `--strict`
- 错误退出码
- 标准输出和标准错误分离

### 大文件回归测试

生成大量重复日志，验证：

- 能够完成处理
- 输出计数正确
- 结果不会随总行数保存原始记录
- 处理过程中不会出现明显的内存线性增长

## 11. 关键边界决策

### 非法行

默认容错，因为日志分析常面对截断文件、混合输出和编码问题。严格模式用于 CI 或数据质量检查。

### 消息规范化

默认关闭。规范化会改变聚合语义，必须由调用者显式启用。

### 时间戳

要求显式时区，统一为 UTC。避免服务器本地时区导致统计结果不一致。

### 日志级别

级别大小写不敏感，但输出使用统一的大写形式。

### 输出稳定性

JSON 字段名称、空值、排序规则和时间格式必须固定，便于下游脚本消费。

### 大量错误类型

第一版支持完整错误聚合；设计上保留最大聚合组数和近似 top-k 的扩展点。若未来必须在严格内存限制下运行，可以引入标准库之外的近似算法，但不应作为第一版默认行为。

## 12. 推荐验收标准

实现完成后至少满足：

1. 可以从普通文件和标准输入逐行处理日志。
2. 处理百万行日志时不保存全部日志内容。
3. 时间过滤遵循 `[from, to)`。
4. 错误消息计数、首次时间和末次时间正确。
5. JSON 输出可被标准 JSON 工具直接解析。
6. 非法行默认不会中断分析，并且会被计数。
7. 严格模式会在非法行时返回非零状态码。
8. 所有核心逻辑可脱离命令行单独测试。
9. 测试覆盖解析、过滤、聚合、输出、错误处理和流式读取。
10. 全部实现只使用 Python 标准库。
