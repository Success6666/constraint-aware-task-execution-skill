# 命令行日志分析器设计

## 1. 目标与使用方式

实现一个名为 `logan` 的命令行程序，支持：

- 流式读取标准输入或一个/多个日志文件
- 按时间范围过滤
- 统计错误数量并按错误类型聚合
- 将结果导出为 JSON
- 使用标准库完成核心功能，尽量不引入第三方依赖
- 通过单元测试、集成测试和命令行测试验证行为

建议命令形式：

```text
logan [OPTIONS] [FILES...]
```

示例：

```bash
logan app.log
cat app.log | logan --from 2025-01-01T00:00:00Z --to 2025-01-02T00:00:00Z
logan --level ERROR --group-by code --json app.log
```

当未提供文件时读取标准输入；使用 `-` 显式表示标准输入。

## 2. 日志输入格式

优先支持结构化 JSON Lines，每行一个 JSON 对象：

```json
{"timestamp":"2025-01-01T12:00:00Z","level":"ERROR","message":"database unavailable","code":"DB_CONN"}
```

字段定义：

- `timestamp`：必需，ISO 8601 时间
- `level`：可选，默认 `INFO`
- `message`：可选，默认为空字符串
- `code`：可选，用于错误聚合
- 其他字段保留在原始记录中，但不影响核心分析

可额外支持常见文本格式，例如：

```text
2025-01-01T12:00:00Z ERROR DB_CONN database unavailable
```

文本解析器应作为独立适配器实现。无法识别的行计入 `malformed_lines`，默认继续处理后续输入；通过 `--strict` 将其视为错误并终止。

## 3. 核心模块

### CLI 层

使用 Python 标准库 `argparse`：

```text
--from TIME       起始时间，包含边界
--to TIME         结束时间，包含边界
--level LEVEL     仅保留指定级别
--group-by FIELD  聚合字段，默认 code；缺失时使用 "UNKNOWN"
--format FORMAT   输出格式：text 或 json，默认 text
--json            --format json 的简写
--strict          遇到无法解析的行立即失败
--timezone TZ     可选时区配置；默认要求输入带时区
```

参数校验规则：

- `from` 与 `to` 必须是合法时间
- `from > to` 时报告参数错误
- `--json` 与 `--format text` 同时出现时以命令行后出现的选项为准，或直接拒绝并保持行为明确
- 级别统一转为大写比较

### 输入层

定义统一迭代接口：

```python
Iterator[RawLine]
```

分别实现：

- `stdin_source()`
- `file_source(paths)`
- `iter_lines(source)`

输入层只负责逐行读取和行号记录，不一次性加载全部内容。使用 `utf-8` 解码；单行解码问题按普通解析错误处理。

### 解析层

定义：

```python
parse_line(raw_line: str, line_number: int) -> ParseResult
```

`LogRecord` 建议包含：

```python
@dataclass
class LogRecord:
    timestamp: datetime
    level: str
    message: str
    code: str | None
    fields: Mapping[str, Any]
```

解析成功返回记录，失败返回包含行号和原因的解析错误。时间统一转换为带时区的 `datetime`，内部统一使用 UTC 比较。

### 过滤层

使用可组合的谓词：

```python
matches(record, query) -> bool
```

过滤顺序：

1. 时间范围
2. 日志级别
3. 其他未来可扩展条件

时间范围采用闭区间：

```text
from <= timestamp <= to
```

### 聚合层

默认只聚合 `ERROR` 记录；若指定 `--level`，则聚合过滤后的目标级别记录。为保证流式处理，只保留计数和必要的最小统计状态：

```python
@dataclass
class GroupStats:
    count: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
```

按 `--group-by` 指定的字段取值；缺失、空值或非标量值统一映射为 `UNKNOWN`。维护：

- `total_lines`
- `parsed_lines`
- `matched_lines`
- `malformed_lines`
- `error_count`
- `groups: dict[str, GroupStats]`

复杂度：单行处理平均 O(1)，空间复杂度为 O(聚合分组数量)，不随输入总行数增长。

## 4. 输出格式

### 文本输出

示例：

```text
Total lines: 10000
Parsed lines: 9980
Matched lines: 320
Malformed lines: 20
Error count: 320

Errors by code:
  DB_CONN: 180
  TIMEOUT: 95
  UNKNOWN: 45
```

分组按数量降序排列，数量相同时按分组名称升序排列，确保输出稳定。

### JSON 输出

输出一个完整 JSON 对象：

```json
{
  "total_lines": 10000,
  "parsed_lines": 9980,
  "matched_lines": 320,
  "malformed_lines": 20,
  "error_count": 320,
  "groups": [
    {
      "key": "DB_CONN",
      "count": 180,
      "first_seen": "2025-01-01T00:01:02Z",
      "last_seen": "2025-01-01T23:59:59Z"
    }
  ]
}
```

使用 `json.dumps(..., ensure_ascii=False, sort_keys=True)`，时间统一输出 ISO 8601 UTC 格式。JSON 输出写到标准输出，诊断信息写到标准错误，避免污染机器可读结果。

## 5. 错误处理与退出码

- `0`：分析完成，即使存在可跳过的坏行
- `1`：输入读取失败、严格模式解析失败或内部处理失败
- `2`：命令行参数错误

非严格模式下，对坏行输出简短诊断信息到标准错误，例如：

```text
warning: line 42: invalid timestamp
```

不在标准输出中混入警告，尤其是 JSON 模式。

## 6. 流式处理主流程

```python
def analyze(lines, query, strict=False):
    result = AnalysisResult()

    for line_number, raw_line in enumerate(lines, start=1):
        result.total_lines += 1
        parsed = parse_line(raw_line, line_number)

        if parsed.is_error:
            result.malformed_lines += 1
            if strict:
                raise ParseFailure(parsed.error)
            continue

        result.parsed_lines += 1
        record = parsed.record
        if not matches(record, query):
            continue

        result.matched_lines += 1
        if record.level == "ERROR":
            result.error_count += 1
            result.add_group(record)

    return result
```

程序入口负责组装参数、输入源、解析器、分析器和渲染器；分析器本身不依赖命令行对象，便于测试和复用。

## 7. 测试方案

### 单元测试

覆盖：

- 合法 JSON 行解析
- 合法文本行解析
- 缺失字段和默认值
- 无效 JSON、无效时间、空行
- 带时区时间转换为 UTC
- 起止时间边界包含规则
- 级别大小写处理
- 分组字段缺失时使用 `UNKNOWN`
- 分组排序稳定性
- 空输入和全为坏行的输入

### 集成测试

通过内存字符串或临时输入流验证：

- 从标准输入读取
- 多文件顺序读取
- 时间过滤和错误聚合组合
- `--strict` 的失败行为
- 文本输出内容
- JSON 输出可被标准库 `json.loads` 重新解析
- 诊断信息只出现在标准错误
- 各类退出码

### 性能测试

生成大规模 JSON Lines 输入，验证：

- 处理过程不将全部日志载入内存
- 内存占用主要取决于分组数量
- 长时间持续输入时可持续输出最终结果
- 大文件和标准输入行为一致

## 8. 实现顺序

1. 定义 `LogRecord`、查询条件和分析结果模型
2. 实现时间解析与 UTC 规范化
3. 实现 JSON Lines 解析器和可选文本解析器
4. 实现流式输入源
5. 实现过滤与聚合
6. 实现文本和 JSON 渲染器
7. 接入 `argparse` 与退出码
8. 补充单元、集成和性能测试

核心实现仅需 Python 标准库中的 `argparse`、`json`、`datetime`、`dataclasses`、`typing`、`sys` 和 `pathlib`，无需额外运行时依赖。
