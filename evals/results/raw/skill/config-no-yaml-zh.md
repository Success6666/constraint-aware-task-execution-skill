## 设计目标

- 配置源仅支持 JSON。
- 优先级：**默认值 < JSON 配置 < 环境变量**。
- 环境变量使用 `APP_` 前缀，嵌套字段用 `__` 分隔。
- 使用 Pydantic 做严格类型校验。
- JSON 中出现未知字段时立即报错；无关环境变量忽略。
- 错误信息包含配置路径、来源和具体原因。

## 配置模型

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    host: str = "127.0.0.1"
    port: int = 5432
    name: str = "app"


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    workers: int = 2
    database: DatabaseConfig = DatabaseConfig()
```

## 环境变量映射

```python
ENV_BINDINGS = {
    "APP_DEBUG": (("debug",), bool),
    "APP_LOG_LEVEL": (("log_level",), str),
    "APP_WORKERS": (("workers",), int),

    "APP_DATABASE__HOST": (("database", "host"), str),
    "APP_DATABASE__PORT": (("database", "port"), int),
    "APP_DATABASE__NAME": (("database", "name"), str),
}
```

环境变量示例：

```text
APP_DEBUG=true
APP_WORKERS=4
APP_DATABASE__HOST=db.internal
APP_DATABASE__PORT=5432
```

非字符串类型使用 JSON 字面量：

- `bool`：`true` 或 `false`
- `int`：`4`
- `list`、`dict`：例如 `["a", "b"]`

## 加载实现

依赖：

```text
pydantic>=2.0
```

实现：

```python
import json
import os
from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter, ValidationError


class ConfigError(ValueError):
    """配置加载或校验失败。"""


def _set_nested(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = target

    for key in path[:-1]:
        child = current.get(key)

        if child is None:
            child = {}
            current[key] = child
        elif not isinstance(child, dict):
            joined = ".".join(path)
            raise ConfigError(
                f"配置路径 {joined!r} 无法覆盖："
                f"{key!r} 不是对象"
            )

        current = child

    current[path[-1]] = value


def _parse_env_value(
    env_name: str,
    raw_value: str,
    annotation: type,
) -> Any:
    # 字符串直接使用，不要求环境变量包含 JSON 引号。
    if annotation is str:
        return raw_value

    try:
        return TypeAdapter(annotation).validate_json(raw_value)
    except ValidationError as exc:
        raise ConfigError(
            f"环境变量 {env_name!r} 的值无效：{exc.errors()[0]['msg']}"
        ) from exc


def _format_validation_error(exc: ValidationError) -> str:
    messages = []

    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"])
        messages.append(f"{location}: {error['msg']}")

    return "；".join(messages)


def load_config(
    json_text: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """
    按默认值 -> JSON -> 环境变量的顺序加载配置。

    json_text=None 表示不提供 JSON 配置。
    """
    data: dict[str, Any] = {}

    if json_text is not None:
        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"JSON 配置无效：第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
            ) from exc

        if not isinstance(parsed, dict):
            raise ConfigError("JSON 配置的顶层必须是对象")

        data = parsed

    env = os.environ if environ is None else environ

    for env_name, (path, annotation) in ENV_BINDINGS.items():
        if env_name in env:
            value = _parse_env_value(
                env_name,
                env[env_name],
                annotation,
            )
            _set_nested(data, path, value)

    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            f"配置校验失败：{_format_validation_error(exc)}"
        ) from exc
```

## JSON 示例

```json
{
  "debug": false,
  "log_level": "INFO",
  "workers": 2,
  "database": {
    "host": "localhost",
    "port": 5432,
    "name": "service"
  }
}
```

环境变量覆盖后：

```text
APP_DATABASE__HOST=db.internal
APP_DATABASE__PORT=5433
```

最终结果中的值为：

```python
config.database.host == "db.internal"
config.database.port == 5433
```

## 验证要求

至少覆盖以下测试场景：

1. 无 JSON、无环境变量时使用全部默认值。
2. JSON 正确覆盖默认值。
3. 环境变量覆盖 JSON。
4. `APP_DATABASE__PORT=not-a-number` 返回包含环境变量名的错误。
5. JSON 中 `workers` 为字符串时返回类型错误。
6. JSON 包含未知字段时返回字段路径。
7. JSON 顶层不是对象时返回明确错误。
8. JSON 语法错误时包含行号和列号。
9. `APP_DEBUG=true` 能解析为 `True`。
10. 未登记的环境变量不会改变配置。
