配置模块可以采用明确的三层优先级：

`默认值 < JSON 文件 < 环境变量`

环境变量使用双下划线表示嵌套字段，例如：

```text
APP_SERVER__PORT=9000
APP_DATABASE__HOST=db.internal
```

下面是一个只依赖 Python 标准库的实现。

```python
# config.py
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ConfigError(Exception):
    """配置错误，包含可读的字段路径和原因。"""


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False


@dataclass(frozen=True)
class DatabaseConfig:
    host: str = "127.0.0.1"
    port: int = 5432
    user: str = "app"
    password: str = ""
    name: str = "app"


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    database: DatabaseConfig


DEFAULTS: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8000,
        "debug": False,
    },
    "database": {
        "host": "127.0.0.1",
        "port": 5432,
        "user": "app",
        "password": "",
        "name": "app",
    },
}

SCHEMA: dict[str, type] = {
    "server.host": str,
    "server.port": int,
    "server.debug": bool,
    "database.host": str,
    "database.port": int,
    "database.user": str,
    "database.password": str,
    "database.name": str,
}


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _parse_bool(value: str, path: str) -> bool:
    normalized = value.strip().lower()

    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    raise ConfigError(
        f"{path}: 无效的布尔值 {value!r}，可使用 true/false"
    )


def _parse_value(value: Any, expected_type: type, path: str) -> Any:
    # JSON 中的值已经是对应类型，只做校验。
    if not isinstance(value, str):
        if expected_type is int and isinstance(value, bool):
            raise ConfigError(f"{path}: 期望整数，实际为布尔值")
        if not isinstance(value, expected_type):
            raise ConfigError(
                f"{path}: 期望 {expected_type.__name__}，"
                f"实际为 {type(value).__name__}"
            )
        return value

    # 环境变量始终是字符串，需要显式转换。
    if expected_type is str:
        return value

    if expected_type is bool:
        return _parse_bool(value, path)

    if expected_type is int:
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"{path}: 无效的整数 {value!r}") from exc

    raise ConfigError(f"{path}: 不支持的配置类型 {expected_type.__name__}")


def _get(data: Mapping[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise ConfigError(f"{path}: 缺少必填配置")
        current = current[part]
    return current


def _set(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = data

    for part in parts[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise ConfigError(f"{path}: 父级配置不是对象")
        current = child

    current[parts[-1]] = value


def _validate_unknown_keys(data: Mapping[str, Any], prefix: str = "") -> None:
    allowed = {
        "server": {"host", "port", "debug"},
        "database": {"host", "port", "user", "password", "name"},
    }

    for section, values in data.items():
        if section not in allowed:
            raise ConfigError(f"{section}: 未知配置段")

        if not isinstance(values, Mapping):
            raise ConfigError(f"{section}: 必须是 JSON 对象")

        unknown = set(values) - allowed[section]
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ConfigError(f"{section}: 未知配置项: {names}")


def load_config(
    config_path: str | Path = "config.json",
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    env = os.environ if environ is None else environ
    data = json.loads(json.dumps(DEFAULTS))

    path = Path(config_path)
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as file:
                json_data = json.load(file)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{path}: JSON 格式错误，第 {exc.lineno} 行第 {exc.colno} 列"
            ) from exc
        except OSError as exc:
            raise ConfigError(f"{path}: 无法读取配置文件: {exc}") from exc

        if not isinstance(json_data, dict):
            raise ConfigError(f"{path}: 顶层必须是 JSON 对象")

        _deep_merge(data, json_data)

    _validate_unknown_keys(data)

    # APP_DATABASE__PORT -> database.port
    for name, raw_value in env.items():
        if not name.startswith("APP_"):
            continue

        config_path = name[4:].lower().replace("__", ".")
        expected_type = SCHEMA.get(config_path)

        # 非配置变量直接忽略，例如 APP_LOG_LEVEL。
        if expected_type is None:
            continue

        parsed = _parse_value(raw_value, expected_type, config_path)
        _set(data, config_path, parsed)

    errors: list[str] = []

    for field_path, expected_type in SCHEMA.items():
        try:
            value = _get(data, field_path)
            _parse_value(value, expected_type, field_path)
        except ConfigError as exc:
            errors.append(str(exc))

    if errors:
        raise ConfigError("配置校验失败:\n- " + "\n- ".join(errors))

    return AppConfig(
        server=ServerConfig(
            host=data["server"]["host"],
            port=data["server"]["port"],
            debug=data["server"]["debug"],
        ),
        database=DatabaseConfig(
            host=data["database"]["host"],
            port=data["database"]["port"],
            user=data["database"]["user"],
            password=data["database"]["password"],
            name=data["database"]["name"],
        ),
    )
```

`config.json` 示例：

```json
{
  "server": {
    "port": 8080,
    "debug": false
  },
  "database": {
    "host": "db.internal",
    "name": "orders"
  }
}
```

使用方式：

```python
from config import ConfigError, load_config

try:
    config = load_config("config.json")
except ConfigError as exc:
    print(f"启动失败: {exc}")
    raise SystemExit(1)

print(config.server.port)
print(config.database.host)
```

建议约定：

- JSON 文件缺失时使用默认值。
- JSON 存在但格式错误时立即失败。
- 环境变量只覆盖已声明字段。
- 整数、布尔值等环境变量必须显式转换。
- 一次校验收集多个错误，便于启动时集中修复。
- 密码等敏感值只从环境变量传入，避免写入 JSON 或日志。