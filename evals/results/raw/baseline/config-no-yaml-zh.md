当前工作区是只读沙箱，文件创建和修改均被系统拒绝，因此无法实际落地代码或测试。

建议模块接口如下：

```python
load_config(
    config_path: str | Path | None = None,
    *,
    schema: Mapping[str, Any] | None = None,
    defaults: Mapping[str, Any] | None = None,
    env_prefix: str = "APP_",
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]
```

设计要点：

- 读取 JSON 文件，要求顶层为对象。
- 合并优先级：`defaults < JSON < 环境变量`。
- 环境变量使用双下划线表达嵌套键，例如 `APP_SERVER__PORT=9000`。
- 支持 `str`、`int`、`float`、`bool`、`list`、`dict` 类型转换。
- 使用专用异常：
  - `ConfigFileError`：文件不存在、无法读取、JSON 格式错误。
  - `ConfigValidationError`：类型错误、缺少必需配置、环境变量值非法。
- 全程只使用 Python 标准库，不解析 YAML。

需要在可写工作区重新执行，才能创建 `config_loader.py`、测试文件和任务书。