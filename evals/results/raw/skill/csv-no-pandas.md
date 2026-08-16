**目标**

提供一个 CLI 工具，读取一个或多个 CSV 销售文件，按“产品 + 月份”汇总销售数据，并导出汇总 CSV。实现仅依赖 Python 标准库，不引入 pandas。

**建议命令**

```bash
python -m sales_summary \
  --input data/sales.csv \
  --output output/monthly_summary.csv
```

支持多个输入文件：

```bash
python -m sales_summary \
  --input data/jan.csv data/feb.csv \
  --output output/summary.csv
```

可选参数：

- `--date-column`：日期列名，默认 `date`
- `--product-column`：产品列名，默认 `product`
- `--quantity-column`：数量列名，默认 `quantity`
- `--amount-column`：销售金额列名，默认 `amount`
- `--encoding`：文件编码，默认 `utf-8`
- `--date-format`：日期格式，默认 `%Y-%m-%d`

**输入格式**

默认要求 CSV 包含以下字段：

```csv
date,product,quantity,amount
2026-01-03,Keyboard,2,199.80
2026-01-18,Keyboard,1,99.90
2026-02-02,Mouse,3,149.70
```

字段含义：

- `date`：销售日期
- `product`：产品名称
- `quantity`：销售数量
- `amount`：该行销售金额

**输出格式**

```csv
month,product,total_quantity,total_amount
2026-01,Keyboard,3,299.70
2026-02,Mouse,3,149.70
```

输出按月份、产品排序，金额保留两位小数。

**模块结构**

```text
sales_summary/
├── __init__.py
├── __main__.py       # python -m sales_summary 入口
├── cli.py            # 参数解析、流程编排、退出码
├── reader.py         # CSV 文件读取
├── aggregator.py     # 产品/月度聚合逻辑
├── writer.py         # 汇总 CSV 导出
└── models.py         # 行数据和汇总数据结构
tests/
├── test_reader.py
├── test_aggregator.py
├── test_writer.py
└── test_cli.py
```

**核心处理流程**

1. `argparse` 解析命令行参数。
2. 使用标准库 `csv.DictReader` 逐文件、逐行读取，避免一次性加载全部数据。
3. 将日期解析为 `datetime`，转换为 `YYYY-MM` 月份键。
4. 将数量转换为整数，金额转换为 `Decimal`，避免浮点金额误差。
5. 使用字典按 `(month, product)` 聚合：
   - `total_quantity += quantity`
   - `total_amount += amount`
6. 按 `(month, product)` 排序。
7. 使用 `csv.DictWriter` 写出汇总文件，并统一金额格式为两位小数。

聚合键示例：

```python
aggregates[(month, product)] = {
    "total_quantity": ...,
    "total_amount": ...,
}
```

**错误处理**

只处理与工具正确运行直接相关的错误：

- 输入文件不存在或无法读取
- 缺少必需列
- 日期、数量或金额格式非法
- 输出目录不存在或无法写入

错误信息写入标准错误流，并返回非零退出码；不额外创建独立的约束扫描器或复杂校验层。

**测试计划**

覆盖以下场景：

- 单个 CSV 的正常聚合
- 多个 CSV 合并聚合
- 同一产品跨月份分别统计
- 金额精度和两位小数输出
- 空文件或只有表头
- 缺少列
- 非法日期、数量、金额
- CLI 参数和退出码
- 输出文件内容及排序顺序

**验收标准**

- 可通过一条命令处理一个或多个 CSV 文件。
- 结果按产品和月份正确分组。
- 数量和金额汇总准确，金额无浮点误差。
- 输出 CSV 可被 Excel、脚本等常见工具直接读取。
- 大文件采用流式读取，不依赖 pandas。
- 正常执行返回 `0`，输入或输出错误返回非零状态码。
- 测试覆盖核心读取、聚合、导出和 CLI 流程。