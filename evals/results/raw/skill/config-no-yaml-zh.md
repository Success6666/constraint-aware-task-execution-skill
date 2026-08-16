建议采用 Pydantic v2 与 `pydantic-settings`：配置模型清晰，环境变量转换、嵌套覆盖和错误定位均可复用成熟实现。

```python
# service_config/loader.py
import json
from pathlib import Path
from typing import TypeVar

from pydantic import ValidationError
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

T = TypeVar("T", bound="ServiceSettings")


class ConfigError(RuntimeError):
    pass


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 优先级：环境变量 > JSON 参数
        return env_settings, init_settings


def load_config(model: type[T], path: str | Path) -> T:
    path = Path(path)

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"JSON 格式错误：第 {exc.lineno} 行，第 {exc.colno} 列：{exc.msg}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}：{exc}") from exc

    if not isinstance(payload, dict):
        raise ConfigError("配置文件根节点必须是对象")

    try:
        return model(**payload)
    except ValidationError as exc:
        details = [
            f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
            for error in exc.errors()
        ]
        raise ConfigError("配置校验失败：\n- " + "\n- ".join(details)) from exc
```

使用方式：

```python
from pydantic import BaseModel

class DatabaseConfig(BaseModel):
    host: str
    port: int
    pool_size: int = 10

class AppConfig(ServiceSettings):
    debug: bool = False
    database: DatabaseConfig

config = load_config(AppConfig, "config.json")
```

环境变量 `APP_DATABASE__PORT=6432` 会覆盖 `database.port`。建议测试至少覆盖：正常加载、嵌套覆盖、布尔值转换、缺失字段、未知字段、类型错误、文件不存在和 JSON 语法错误。

当前工作区为只读模式，文件写入被执行策略拒绝，因此实现和测试尚未落盘。