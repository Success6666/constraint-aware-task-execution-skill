建议用 Python 3.11+ 实现，核心功能完全复用标准库：`argparse`、`datetime`、`json`、`re`、`collections`、`pathlib`、`gzip`、`unittest`。按行迭代文件，不将完整日志载入内存。

**命令行接口**

```bash
log-analyzer app.log \
  --from "2026-08-16T09:00:00+08:00" \
  --to   "2026-08-16T18:00:00+08:00" \
  --level ERROR \
  --format text \
  --output report.json
```

支持标准输入和压缩文件：

```bash
journalctl -u api.service | log-analyzer -
log-analyzer archived.log.gz --format jsonl
```

主要参数：

```text
INPUT                 日志文件、.gz 文件或 -（stdin）
--from TIMESTAMP      起始时间，包含
--to TIMESTAMP        结束时间，不包含
--level LEVEL         最低日志级别，默认 ERROR
--format auto|text|jsonl
--timestamp-field     JSONL 时间字段，默认 timestamp
--message-field       JSONL 消息字段，默认 message
--group-by fingerprint|message
--top N               最多输出 N 个错误组，默认全部
--output PATH         JSON 输出路径，省略时写 stdout
--strict              遇到无法解析的行立即失败
```

时间范围采用半开区间 `[from, to)`，可避免相邻时间段统计重复。所有带时区的时间统一转换为 UTC 后比较；无时区时间通过 `--timezone` 指定固定偏移，例如 `+08:00`，不依赖时区数据库。

**处理流程**

```text
文件/stdin
   ↓ 按行迭代
解析日志记录
   ↓
时间范围与级别过滤
   ↓
错误指纹归一化
   ↓
Counter 聚合 + 首末时间记录
   ↓
JSON 序列化
```

建议的数据结构：

```python
@dataclass(frozen=True)
class LogRecord:
    timestamp: datetime
    level: str
    message: str
    source: str
    line_number: int
```

解析器只负责把一行转换成 `LogRecord | None`：

```python
class LogParser(Protocol):
    def parse(self, line: str, source: str, line_number: int) -> LogRecord | None:
        ...
```

首版提供两种解析器：

- `JsonLineParser`：用 `json.loads()` 读取每行 JSON。
- `TextLineParser`：解析明确约定的文本格式，例如
  `2026-08-16T10:20:30+08:00 ERROR database connection failed`
- `auto`：根据首个非空行是否为 JSON 对整个输入选择解析器，不逐行反复探测。

流式读取：

```python
def iter_lines(path: str) -> Iterator[tuple[str, int, str]]:
    # 返回 source、line_number、line
```

普通文件使用 `open(..., encoding="utf-8", errors="replace")`，`.gz` 使用 `gzip.open()`，`-` 使用 `sys.stdin`。

**错误聚合**

直接按完整消息分组通常会被请求 ID、数字和地址打散。默认生成轻量指纹：

```text
User 1842 failed request 550e8400-e29b-41d4-a716-446655440000
→ User <num> failed request <uuid>

Timeout connecting to 10.1.2.3:5432 after 3000ms
→ Timeout connecting to <ip>:<num> after <num>ms
```

归一化规则限定为常见动态值：

- UUID
- IPv4 地址
- 十六进制地址
- 独立数字

不要默认替换路径、类名或普通单词，以免把不同问题错误合并。每个聚合项记录：

```python
@dataclass
class ErrorGroup:
    fingerprint: str
    count: int
    first_seen: datetime
    last_seen: datetime
    sample: str
```

内存占用约为 `O(错误组数量)`，而不是 `O(日志行数)`。输出按 `count` 降序，再按 `fingerprint` 排序，保证结果稳定。

**JSON 输出**

```json
{
  "schema_version": 1,
  "input": "app.log",
  "range": {
    "from": "2026-08-16T01:00:00Z",
    "to": "2026-08-16T10:00:00Z"
  },
  "summary": {
    "lines_read": 1250000,
    "records_parsed": 1249980,
    "records_matched": 386,
    "parse_errors": 20,
    "error_groups": 12
  },
  "errors": [
    {
      "fingerprint": "Timeout connecting to <ip>:<num> after <num>ms",
      "count": 173,
      "first_seen": "2026-08-16T01:04:12Z",
      "last_seen": "2026-08-16T09:58:41Z",
      "sample": "Timeout connecting to 10.1.2.3:5432 after 3000ms"
    }
  ]
}
```

默认对坏行计数并继续；`--strict` 才返回非零退出码。建议退出码：

- `0`：成功
- `2`：命令行参数无效
- `3`：输入或输出失败
- `4`：严格模式下日志解析失败

**项目结构**

```text
log_analyzer/
  __init__.py
  __main__.py
  cli.py
  input.py
  parsers.py
  filters.py
  aggregate.py
  report.py
tests/
  test_parsers.py
  test_filters.py
  test_aggregate.py
  test_cli.py
```

保持模块职责直接，不需要插件框架或第三方依赖。通过 `python -m log_analyzer` 运行；如需安装成命令，可在 `pyproject.toml` 声明：

```toml
[project.scripts]
log-analyzer = "log_analyzer.cli:main"
```

**测试重点**

使用 `unittest`、`tempfile`、`io.StringIO` 和 `subprocess`：

- 文本与 JSONL 正常解析
- 空行、非法 JSON、缺少字段及无效时间戳
- 起始时间包含、结束时间排除
- 不同时区表示同一时刻
- 日志级别排序和过滤
- UUID、IP、数字指纹归一化
- 聚合数量、首末时间和稳定排序
- stdin、普通文件与 gzip 输入
- stdout 和文件 JSON 输出
- 宽字符及无效 UTF-8 的处理
- `--strict` 与各退出码
- 大型生成器输入，确认实现只迭代一次、不调用 `read()` 或 `readlines()`

这种设计能覆盖流式读取、时间过滤、错误聚合、JSON 导出和端到端测试，同时运行时无需增加第三方依赖。
