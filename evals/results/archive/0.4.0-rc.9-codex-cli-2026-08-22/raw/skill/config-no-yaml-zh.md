下面是一种轻量、可测试的配置加载设计，只使用 Python 标准库和 JSON。

```python
# config.py
from __future__ import annotations

import json
import os
from dataclasses import MISSING, dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints


class ConfigError(ValueError):
    """配置文件或环境变量不合法。"""


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False


@dataclass(frozen=True)
class DatabaseConfig:
    url: str
    pool_size: int = 10


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    database: DatabaseConfig


def _parse_value(value: Any, expected_type: Any, key: str) -> Any:
    origin = get_origin(expected_type)
    args = get_args(expected_type)

    if value is None:
        if expected_type is type(None):
            return None
        raise ConfigError(f"{key}: 不允许为空")

    if origin is list:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = [item.strip() for item in value.split(",") if item.strip()]

        if not isinstance(value, list):
            raise ConfigError(f"{key}: 需要列表，实际为 {type(value).__name__}")

        item_type = args[0] if args else Any
        return [
            _parse_value(item, item_type, f"{key}[{index}]")
            for index, item in enumerate(value)
        ]

    if expected_type is bool:
        if isinstance(value, bool):
            return value

        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False

        raise ConfigError(f"{key}: 需要布尔值 true/false，实际为 {value!r}")

    if expected_type is int:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{key}: 需要整数，实际为 {value!r}") from None

    if expected_type is float:
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{key}: 需要数字，实际为 {value!r}") from None

    if expected_type is str:
        if not isinstance(value, str):
            raise ConfigError(f"{key}: 需要字符串，实际为 {type(value).__name__}")
        return value

    return value


def _build_config(cls: type, data: dict[str, Any], path: str = "") -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"{path or 'root'}: 需要 JSON 对象")

    hints = get_type_hints(cls)
    known_fields = {field.name for field in fields(cls)}

    unknown = set(data) - known_fields
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ConfigError(f"{path or 'root'}: 未知配置项: {names}")

    values = {}

    for field in fields(cls):
        key = f"{path}.{field.name}" if path else field.name
        field_type = hints[field.name]

        if field.name not in data:
            if field.default is not MISSING or field.default_factory is not MISSING:
                continue
            raise ConfigError(f"{key}: 缺少必填配置")

        raw_value = data[field.name]

        if is_dataclass(field_type):
            values[field.name] = _build_config(field_type, raw_value, key)
        else:
            values[field.name] = _parse_value(raw_value, field_type, key)

    return cls(**values)


def _set_nested(data: dict[str, Any], path: list[str], value: str) -> None:
    current = data

    for part in path[:-1]:
        current = current.setdefault(part, {})

    current[path[-1]] = value


def load_config(
    config_path: str | Path,
    *,
    env: dict[str, str] | None = None,
    env_prefix: str = "APP_",
) -> AppConfig:
    env = os.environ if env is None else env

    try:
        raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"配置文件不存在: {config_path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"JSON 配置格式错误: 第 {exc.lineno} 行，第 {exc.colno} 列: {exc.msg}"
        ) from None

    if not isinstance(raw, dict):
        raise ConfigError("配置文件根节点必须是 JSON 对象")

    merged = dict(raw)

    # APP_SERVER__PORT=9000 -> {"server": {"port": "9000"}}
    for name, value in env.items():
        if not name.startswith(env_prefix):
            continue

        path = name[len(env_prefix):].lower().split("__")
        if any(not part for part in path):
            raise ConfigError(f"环境变量名非法: {name}")

        _set_nested(merged, path, value)

    return _build_config(AppConfig, merged)
```

JSON 示例：

```json
{
  "server": {
    "host": "127.0.0.1",
    "port": 8000,
    "debug": false
  },
  "database": {
    "url": "postgresql://localhost/app",
    "pool_size": 10
  }
}
```

环境变量覆盖：

```text
APP_SERVER__PORT=9000
APP_SERVER__DEBUG=true
APP_DATABASE__POOL_SIZE=20
```

使用方式：

```python
try:
    config = load_config("config.json")
except ConfigError as exc:
    print(f"配置错误: {exc}")
    raise SystemExit(2)
```

设计要点：

- 加载顺序为：JSON 默认值，再由环境变量覆盖。
- 环境变量使用 `APP_` 前缀，嵌套字段使用双下划线。
- 支持 `str`、`int`、`float`、`bool` 和 `list[T]` 类型转换。
- 聚焦于启动阶段失败，错误信息包含字段路径，例如 `server.port`。
- 拒绝未知 JSON 配置项，避免拼写错误被静默忽略。
- 通过传入 `env` 字典即可在单元测试中验证覆盖和错误场景。