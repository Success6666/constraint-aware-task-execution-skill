建议使用 **Pydantic v2** 完成类型校验，并把加载流程固定为：

```text
模型默认值 < JSON 文件 < 环境变量
```

环境变量采用统一前缀和双下划线表示嵌套字段，例如：

```text
MYAPP_HOST=0.0.0.0
MYAPP_DATABASE__PORT=5433
MYAPP_DEBUG=true
```

```python
# config.py
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    username: str
    password: str
    pool_size: int = Field(default=10, ge=1)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    debug: bool = False
    database: DatabaseConfig


class ConfigError(RuntimeError):
    """Raised when application configuration cannot be loaded."""


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}")

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"JSON 格式错误：{path}:{exc.lineno}:{exc.colno}，{exc.msg}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}：{exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是 JSON 对象：{path}")

    return data


def _parse_env_value(value: str) -> Any:
    # 借助 JSON 语义转换布尔值、数字、null、数组和对象。
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _read_environment(prefix: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    normalized_prefix = prefix.upper()

    for name, raw_value in os.environ.items():
        if not name.upper().startswith(normalized_prefix):
            continue

        key = name[len(prefix):].lower()
        if not key:
            continue

        parts = key.split("__")
        current = result

        for part in parts[:-1]:
            child = current.setdefault(part, {})
            if not isinstance(child, dict):
                raise ConfigError(f"环境变量路径冲突：{name}")
            current = child

        current[parts[-1]] = _parse_env_value(raw_value)

    return result


def _deep_merge(
    base: dict[str, Any],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    result = base.copy()

    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value

    return result


def _format_validation_error(exc: ValidationError) -> str:
    messages = ["配置校验失败："]

    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"])
        value = error.get("input")
        messages.append(f"- {field}: {error['msg']}（收到：{value!r}）")

    return "\n".join(messages)


def load_config(
    path: str | Path,
    *,
    env_prefix: str = "MYAPP_",
) -> AppConfig:
    file_values = _read_json(Path(path))
    env_values = _read_environment(env_prefix)
    merged_values = _deep_merge(file_values, env_values)

    try:
        return AppConfig.model_validate(merged_values)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc
```

JSON 配置示例：

```json
{
  "host": "127.0.0.1",
  "port": 8000,
  "debug": false,
  "database": {
    "host": "db.internal",
    "port": 5432,
    "username": "service_user",
    "password": "secret",
    "pool_size": 20
  }
}
```

使用方式：

```python
try:
    config = load_config("config.json")
except ConfigError as exc:
    raise SystemExit(str(exc))

print(config.database.host)
```

关键约束：

- `extra="forbid"` 会把拼错或未声明的配置项直接报错。
- 环境变量仅覆盖明确提供的字段，不会覆盖整个嵌套对象。
- 密码等敏感字段应通过环境变量或密钥服务注入，生产配置文件中不要保存明文。
- 单元测试至少覆盖非法 JSON、缺失必填项、未知字段、错误类型、嵌套覆盖和端口越界。