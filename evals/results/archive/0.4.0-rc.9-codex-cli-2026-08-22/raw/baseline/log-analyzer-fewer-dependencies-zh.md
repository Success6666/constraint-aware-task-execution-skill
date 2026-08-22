建议用 Python 标准库实现一个单文件可扩展 CLI，依赖保持为 0：

```text
log-analyzer/
├─ log_analyzer/
│  ├─ __main__.py       # python -m log_analyzer
│  ├─ cli.py            # argparse、退出码
│  ├─ reader.py         # 流式读取 stdin/文件
│  ├─ parser.py         # 日志解析
│  ├─ filter.py         # 时间范围过滤
│  ├─ aggregate.py      # 错误聚合
│  └─ output.py         # 文本/JSON 输出
└─ tests/
   ├─ test_parser.py
   ├─ test_filter.py
   ├─ test_aggregate.py
   └─ test_cli.py
```

核心数据结构：

```python
@dataclass
class LogRecord:
    timestamp: datetime
    level: str
    message: str
    source: str | None = None
    raw: str = ""
```

默认支持常见格式，例如：

```text
2026-08-22T10:15:30Z ERROR database connection failed
2026-08-22 10:16:02,123 WARN retrying request
```

解析器按优先级尝试：

1. JSON 日志：使用 `json.loads`
2. 结构化文本：正则提取时间、级别和消息
3. 未知格式：记录为解析失败，可配置为忽略或报错

流式处理流程：

```text
输入文件/stdin
    -> 逐行读取
    -> 解析
    -> 时间过滤
    -> 错误聚合
    -> 输出结果
```

始终使用迭代器，不将整个日志文件载入内存：

```python
def iter_records(stream):
    for line in stream:
        record = parse_line(line)
        if record is not None:
            yield record
```

CLI 示例：

```bash
python -m log_analyzer app.log
```

```bash
cat app.log | python -m log_analyzer --level ERROR
```

```bash
python -m log_analyzer app.log \
  --from "2026-08-22T10:00:00Z" \
  --to "2026-08-22T11:00:00Z"
```

```bash
python -m log_analyzer app.log \
  --level ERROR \
  --group-by "message" \
  --json result.json
```

建议参数：

```text
--from TIME          包含该时间点
--to TIME            不包含该时间点
--level LEVEL        DEBUG/INFO/WARN/ERROR/FATAL
--group-by FIELD     message、exception、source
--json PATH          导出 JSON；使用 - 输出到 stdout
--format FORMAT      auto、text、json
--ignore-malformed   忽略无法解析的行
--encoding ENCODING  默认 utf-8
```

错误聚合结果：

```json
{
  "summary": {
    "total_lines": 12000,
    "parsed_lines": 11980,
    "matched_lines": 530,
    "malformed_lines": 20
  },
  "errors": [
    {
      "key": "database connection failed",
      "count": 42,
      "first_seen": "2026-08-22T10:01:03Z",
      "last_seen": "2026-08-22T10:59:48Z",
      "samples": [
        "database connection failed"
      ]
    }
  ]
}
```

聚合器使用 `collections.Counter` 和字典保存首次/末次出现时间；采样消息限制数量，避免异常长日志导致内存增长：

```python
groups[key] = {
    "count": groups[key]["count"] + 1,
    "first_seen": min(...),
    "last_seen": max(...),
    "samples": ...
}
```

实现时应明确：

- 时间统一转换为带时区的 `datetime`
- `--from`、`--to` 使用半开区间 `[from, to)`
- 输出 JSON 时通过 `datetime.isoformat()` 序列化
- Broken pipe、文件不存在、参数错误分别返回不同退出码
- 解析失败默认统计并继续处理；严格模式再返回失败

测试重点：

- 各种时间格式和时区解析
- 时间边界包含/排除行为
- ERROR/FATAL 过滤
- 相同错误的聚合计数和首末时间
- stdin 输入和大文件流式处理
- JSON 输出结构稳定
- malformed 行的忽略与严格模式
- CLI 参数错误和退出码

测试框架可直接使用标准库 `unittest`；仅在项目已有约定时再引入 pytest。性能测试可生成临时大文件，验证处理期间内存不会随文件总大小线性增长。