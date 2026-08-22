可以采用“三层配置 + Pydantic 校验”的设计：

- 默认值
- JSON 文件覆盖默认值
- 环境变量覆盖 JSON
- 最终统一进行类型和约束校验

优先级：`默认值 < JSON < 环境变量`。环境变量使用 `APP_` 前缀，嵌套字段用 `__` 分隔，例如 `APP_DATABASE__PORT`。

```python
# config.py
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    user: str
    password: str


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_name: str = "my-service"
    debug: bool = False
    port: int = Field(default=8000, ge=1, le=65535)
    database: DatabaseConfig


class ConfigError(Exception):
    """配置加载或校验失败。"""


def _parse_env_value(value: str) -> Any:
    """
    优先按 JSON 解析，使环境变量支持：
    true、false、123、12.5、["a", "b"] 等类型。
    """
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _set_nested(data: dict[str, Any], path: list[str], value: Any) -> None:
    current = data

    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child

    current[path[-1]] = value


def _load_json(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)

    if not file_path.exists():
        return {}

    try:
        content = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"配置文件 {file_path} 不是合法 JSON："
            f"第 {exc.lineno} 行，第 {exc.colno} 列"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {file_path}：{exc}") from exc

    if not isinstance(content, dict):
        raise ConfigError(f"配置文件 {file_path} 的根节点必须是 JSON 对象")

    return content


def load_settings(
    json_path: str | Path = "config.json",
    environ: Mapping[str, str] | None = None,
) -> Settings:
    env = environ if environ is not None else os.environ

    raw: dict[str, Any] = _load_json(json_path)

    for name, value in env.items():
        if not name.startswith("APP_"):
            continue

        # APP_DATABASE__HOST -> ["database", "host"]
        path = name.removeprefix("APP_").split("__")
        path = [part.lower() for part in path]

        if any(not part for part in path):
            raise ConfigError(f"环境变量 {name} 的字段路径无效")

        _set_nested(raw, path, _parse_env_value(value))

    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        messages = []

        for error in exc.errors():
            location = ".".join(str(item) for item in error["loc"])
            messages.append(f"{location}: {error['msg']}")

        raise ConfigError("配置校验失败：\n- " + "\n- ".join(messages)) from exc
```

示例 `config.json`：

```json
{
  "port": 8080,
  "database": {
    "user": "service",
    "password": "secret"
  }
}
```

环境变量覆盖：

```text
APP_DEBUG=true
APP_PORT=9000
APP_DATABASE__HOST=db.internal
APP_DATABASE__PORT=5432
```

使用方式：

```python
from config import ConfigError, load_settings

try:
    settings = load_settings("config.json")
except ConfigError as exc:
    raise SystemExit(str(exc))

print(settings.database.host)
```

依赖：

```text
pydantic>=2.0
```

该设计不解析 YAML，只接受 JSON；未知字段会被拒绝，端口范围、布尔值、整数等类型错误都会以字段路径形式报告。