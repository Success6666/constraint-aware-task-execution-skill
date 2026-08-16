建议实现为一个 Python 命令行工具，仅依赖标准库，采用“解析 → 过滤 → 聚合 → 输出”的流式管道。

**目标与假设**

- 输入：一个或多个文本日志文件，也支持 `-` 从 stdin 读取。
- 默认日志格式：

```text
2026-08-16T10:12:03Z ERROR database connection failed
```

- 日志级别至少支持 `DEBUG/INFO/WARN/WARNING/ERROR/FATAL`。
- 无法解析的行不应中断处理，可计入 `unparsed` 统计并通过 stderr 提示。

**命令行接口**

```bash
python -m log_analyzer logs/app.log
python -m log_analyzer logs/*.log \
  --from 2026-08-16T10:00:00Z \
  --to 2026-08-16T11:00:00Z \
  --level ERROR \
  --top 20 \
  --json report.json
```

建议参数：

```text
files                 输入文件，使用 - 表示 stdin
--from TIME           起始时间，包含
--to TIME             结束时间，不包含
--level LEVEL         最低日志级别
--pattern REGEX       错误消息归一化规则，可选
--top N               输出前 N 个错误
--json PATH           导出 JSON；使用 - 输出到 stdout
--encoding NAME       默认 utf-8
--fail-on-unparsed    存在无法解析的行时返回非零退出码
```

时间建议统一转换为带时区的 `datetime`。输入没有时区时，应通过 `--timezone` 指定，或明确按本地时区处理，避免静默混用。

**核心模块**

```text
log_analyzer/
  __main__.py       CLI 入口和退出码
  cli.py            argparse 参数定义
  model.py          LogRecord、AnalysisResult 数据类
  parser.py         日志行解析
  pipeline.py       流式读取、过滤、聚合
  normalize.py      错误消息归一化
  output.py         人类可读和 JSON 输出
tests/
  test_parser.py
  test_filter.py
  test_aggregator.py
  test_cli.py
```

推荐使用：

- `argparse`：命令行参数
- `dataclasses`：结构化记录
- `datetime`：时间解析和比较
- `re`：日志解析和消息归一化
- `collections.Counter`：错误计数
- `json`：JSON 导出
- `pathlib`：文件处理
- `unittest`、`unittest.mock`：测试
- `gzip`：可选支持 `.gz`，仍属于标准库

核心数据结构：

```python
@dataclass
class LogRecord:
    timestamp: datetime
    level: str
    message: str
    source: str
    line_number: int
```

```python
@dataclass
class AnalysisResult:
    total_lines: int
    parsed_lines: int
    unparsed_lines: int
    level_counts: dict[str, int]
    error_counts: Counter[str]
    first_timestamp: datetime | None
    last_timestamp: datetime | None
```

**流式处理逻辑**

```python
for source in sources:
    for line_number, line in enumerate(source, start=1):
        record = parser.parse(line)

        if record is None:
            result.unparsed_lines += 1
            continue

        if start_time and record.timestamp < start_time:
            continue
        if end_time and record.timestamp >= end_time:
            continue
        if not level_at_least(record.level, minimum_level):
            continue

        result.parsed_lines += 1
        result.level_counts[record.level] += 1

        if record.level in {"ERROR", "FATAL"}:
            key = normalizer(record.message)
            result.error_counts[key] += 1
```

这样内存占用主要取决于错误聚合键数量，而不是日志总量。对于超大规模、错误种类极多的日志，可以增加：

```text
--max-error-keys N
```

超过上限后归入 `"<other>"`，防止聚合字典无限增长。

**错误聚合**

默认按完整消息统计。可选归一化应处理常见动态字段，例如：

```text
request 12345 failed
request 67890 failed
```

归一化为：

```text
request <number> failed
```

建议只提供有限、可预测的规则：

- 数字替换为 `<number>`
- UUID 替换为 `<uuid>`
- IP 替换为 `<ip>`
- 用户自定义 `--pattern REGEX`

不要默认进行过于激进的模糊聚类，否则会把不同错误合并。

**JSON 输出**

示例：

```json
{
  "summary": {
    "total_lines": 120034,
    "parsed_lines": 119980,
    "unparsed_lines": 54,
    "first_timestamp": "2026-08-16T10:00:00+00:00",
    "last_timestamp": "2026-08-16T10:59:59+00:00"
  },
  "levels": {
    "INFO": 110000,
    "WARN": 7000,
    "ERROR": 2900,
    "FATAL": 80
  },
  "errors": [
    {
      "message": "database connection failed",
      "count": 124
    }
  ]
}
```

JSON 应通过 `json.dump(..., ensure_ascii=False, indent=2)` 写出；时间统一使用 ISO 8601 字符串。人类可读模式则输出摘要、级别统计和 Top 错误。

**退出码**

```text
0 处理成功
1 参数错误或输入文件无法打开
2 存在无法解析的日志行（仅 --fail-on-unparsed）
```

**测试重点**

- 标准日志行解析
- 不同时间格式、时区和边界条件
- `--from` 包含、`--to` 不包含
- 日志级别过滤
- 未解析行不会中断流式处理
- 多文件统计合并
- 错误消息归一化
- Top N 排序和相同计数时的稳定排序
- stdin 输入
- JSON 输出结构和时间序列化
- 空文件、全是无效行、文件不存在
- 大量输入下不按文件总大小增长内存

测试优先使用 `unittest` 和 `io.StringIO`，CLI 测试使用 `subprocess` 或直接调用 `main(argv)`，无需引入 pytest。依赖控制为零第三方依赖；只有在未来需要高性能正则、复杂日志格式或并行处理时再评估增加依赖。