设计建议：实现一个零第三方依赖的 Python CLI 工具，使用 `argparse`、`datetime`、`json`、`re`、`collections` 和 `unittest`。

### CLI

```bash
log-analyzer app.log \
  --from "2026-08-01T00:00:00" \
  --to "2026-08-31T23:59:59" \
  --format json \
  --output report.json
```

建议参数：

- `files`：一个或多个日志文件，支持 `-` 表示 stdin
- `--from`、`--to`：时间范围，采用左闭右开 `[from, to)`
- `--format text|json`：输出格式
- `--output PATH`：输出文件，默认 stdout
- `--timezone`：可选时区配置
- `--error-key message|template`：错误聚合方式

### 处理流程

```text
输入流
  -> 逐行读取
  -> 解析时间、级别、消息
  -> 时间范围过滤
  -> 错误聚合
  -> 输出摘要或 JSON
```

核心接口可以保持简单：

```python
@dataclass
class LogRecord:
    timestamp: datetime
    level: str
    message: str
    source: str | None = None

class LogParser:
    def parse(self, line: str) -> LogRecord | None: ...

class Analyzer:
    def consume(self, records: Iterable[LogRecord]) -> None: ...
    def report(self) -> dict: ...
```

### 流式读取

使用：

```python
with open(path, "r", encoding="utf-8", errors="replace") as f:
    for line_number, line in enumerate(f, 1):
        record = parser.parse(line)
        if record is not None:
            analyzer.consume(record)
```

stdin 使用 `sys.stdin`，避免一次性加载整个文件。内存复杂度主要取决于错误类型数量，而不是日志总行数。

### 日志解析

默认支持类似格式：

```text
2026-08-22 14:30:01 ERROR database connection failed
```

通过预编译正则提取：

- 时间戳
- 日志级别
- 消息正文

解析失败的行计入 `unparsed_lines`，不应导致整个分析终止。时间解析使用 `datetime.strptime`，必要时再扩展 ISO 8601 解析。

### 错误聚合

只聚合 `ERROR` 和 `CRITICAL`：

```python
error_counts: Counter[str]
```

推荐支持两种 key：

1. `message`：完整错误消息
2. `template`：将数字、UUID、路径等动态值归一化后聚合

例如：

```text
request 123 failed for user 456
request 789 failed for user 222
```

归一化为：

```text
request <number> failed for user <number>
```

同时统计：

- 总行数
- 成功解析行数
- 未解析行数
- 各级别数量
- 错误总数
- 错误类型及出现次数
- 首次和最后一次错误时间

### JSON 输出

```json
{
  "summary": {
    "total_lines": 12000,
    "parsed_lines": 11980,
    "unparsed_lines": 20,
    "levels": {
      "INFO": 11000,
      "WARN": 700,
      "ERROR": 250,
      "CRITICAL": 30
    },
    "error_count": 280,
    "first_timestamp": "2026-08-01T00:00:01",
    "last_timestamp": "2026-08-31T23:59:59"
  },
  "errors": [
    {
      "message": "database connection failed",
      "count": 120
    }
  ]
}
```

输出时使用：

```python
json.dump(result, output, ensure_ascii=False, indent=2, default=str)
```

错误列表按 `count` 降序、消息字典序排序，保证结果稳定，便于测试和 diff。

### 测试

使用标准库 `unittest`，覆盖：

- 正常日志解析
- 非法日志行
- 时间范围边界
- 多文件和 stdin 输入
- 错误聚合
- 动态参数归一化
- 空文件
- UTF-8 异常字符
- JSON 输出结构和排序
- CLI 返回码及输出内容

测试输入可使用 `tempfile.NamedTemporaryFile`，CLI 测试使用 `subprocess.run`。同时加入一个小型端到端测试，验证“读取文件、过滤、聚合、导出”完整流程。

### 项目结构

```text
log_analyzer/
  __init__.py
  __main__.py       # python -m log_analyzer
  cli.py            # argparse 和退出码
  parser.py         # 日志格式解析
  analyzer.py       # 流式统计和聚合
  output.py         # text/json 输出
tests/
  test_parser.py
  test_analyzer.py
  test_cli.py
pyproject.toml
README.md
```

`pyproject.toml` 中不声明运行时依赖；测试也只使用标准库。