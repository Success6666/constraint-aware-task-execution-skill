建议使用 Python 标准库实现，完全不引入 pandas。

**目标**

读取销售明细 CSV，按“产品 + 月份”汇总，计算销售数量和销售金额，并导出汇总 CSV。

**输入约定**

原始 CSV 至少包含以下列：

- `product`：产品名称
- `date`：销售日期，支持 `YYYY-MM-DD`
- `quantity`：销售数量
- `amount`：该行销售金额

示例：

```csv
product,date,quantity,amount
Keyboard,2026-01-05,2,199.98
Keyboard,2026-01-18,1,99.99
Mouse,2026-01-20,3,75.00
```

如果实际文件使用 `unit_price` 而不是 `amount`，可在实现阶段增加单价乘数量的计算规则，但应先确认字段定义。

**命令行接口**

```bash
python -m sales_summary input.csv -o summary.csv
```

建议参数：

```text
input                  输入 CSV 文件
-o, --output           输出汇总 CSV 文件
--encoding             文件编码，默认 utf-8-sig
--date-format          日期格式，默认 %Y-%m-%d
```

**处理流程**

1. 使用 `argparse` 解析命令行参数。
2. 使用 `csv.DictReader` 读取文件并校验表头。
3. 逐行解析：
   - 产品名称去除首尾空白；
   - 日期转换为 `YYYY-MM`；
   - 数量转为整数或 Decimal；
   - 金额使用 `Decimal`，避免浮点误差。
4. 使用字典按 `(product, month)` 聚合：
   - `total_quantity`
   - `total_amount`
5. 按月份、产品名称排序，保证输出稳定。
6. 使用 `csv.DictWriter` 导出结果。

**输出格式**

```csv
product,month,total_quantity,total_amount
Keyboard,2026-01,3,299.97
Mouse,2026-01,3,75.00
```

金额统一保留两位小数。

**建议目录结构**

```text
sales-summary/
├── sales_summary/
│   ├── __init__.py
│   ├── cli.py          # 命令行入口
│   ├── parser.py       # CSV 读取与字段校验
│   ├── aggregator.py   # 分组与汇总逻辑
│   └── exporter.py     # 汇总 CSV 输出
├── tests/
│   ├── test_parser.py
│   ├── test_aggregator.py
│   └── test_cli.py
└── README.md
```

如果功能非常小，也可以先合并为单个 `sales_summary.py`，待测试和字段规则稳定后再拆分模块。

**错误处理**

命令行程序应明确报告：

- 输入文件不存在；
- 缺少必需列；
- 日期格式错误及行号；
- 数量或金额不是合法数字；
- 输出文件无法写入。

错误信息应写入 stderr，并返回非零退出码。

**测试计划**

覆盖以下场景：

- 多个产品、多个月份的正常汇总；
- 同一产品同一月份的多行合并；
- 金额精确计算；
- 空 CSV 和缺少表头；
- 非法日期、数量、金额；
- CLI 输出文件内容和退出码；
- UTF-8 BOM 文件读取。

**验收标准**

1. 不依赖 pandas 或其他数据分析框架。
2. 正常输入可以生成结构稳定、排序确定的汇总 CSV。
3. 金额不存在浮点精度误差。
4. 非法输入能指出具体行号和原因。
5. 测试覆盖核心聚合逻辑及主要 CLI 错误路径。
6. README 包含安装、运行命令、输入输出示例和字段约定。

当前仓库为空，因此下一步应先确认输入字段是否确实为 `product/date/quantity/amount`，然后再创建最小 Python CLI 和测试。