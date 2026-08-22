**配置加载模块设计**

### 1. 配置来源与优先级

配置按以下优先级合并，优先级高的覆盖低的：

1. 代码内置默认值
2. JSON 配置
3. 环境变量
4. 显式运行时参数（可选）

建议只支持一个 JSON 配置对象，避免多文件合并导致行为不明确。

```json
{
  "app": {
    "name": "order-service",
    "debug": false
  },
  "server": {
    "host": "0.0.0.0",
    "port": 8080
  },
  "database": {
    "url": "postgresql://localhost/orders",
    "pool_size": 10
  },
  "features": {
    "enable_metrics": true
  }
}
```

---

### 2. 类型安全的配置模型

使用 `dataclasses` 或 `pydantic` 定义配置结构。推荐 Pydantic，因为它天然支持类型转换、嵌套模型和清晰的错误路径。

```python
from pydantic import BaseModel, Field, SecretStr


class AppConfig(BaseModel):
    name: str = "python-service"
    debug: bool = False


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)


class DatabaseConfig(BaseModel):
    url: SecretStr
    pool_size: int = Field(default=10, ge=1, le=100)


class FeatureConfig(BaseModel):
    enable_metrics: bool = True


class Settings(BaseModel):
    app: AppConfig = AppConfig()
    server: ServerConfig = ServerConfig()
    database: DatabaseConfig
    features: FeatureConfig = FeatureConfig()
```

建议：

- 端口限制在 `1..65535`
- 连接池大小限制合理范围
- 密码、Token、数据库连接串使用 `SecretStr`
- 必填项不提供默认值
- 不接受配置模型之外的未知字段，防止拼写错误被静默忽略

在 Pydantic v2 中可额外配置：

```python
from pydantic import ConfigDict


class StrictSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
```

所有配置模型都应继承一个带有 `extra="forbid"` 的基类。

---

### 3. JSON 加载规则

加载流程：

1. 未指定 JSON 路径时使用默认配置
2. 指定路径不存在时直接报错
3. JSON 语法错误时报告行号和列号
4. 根节点必须是 JSON 对象
5. 不接受数组、字符串、数字或 `null` 作为根配置
6. JSON 中的值必须保持原始类型，最终由配置模型统一校验
7. 禁止执行 JSON 中的代码或导入动态对象

接口：

```python
def load_settings(
    config_path: str | None = None,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> Settings:
    ...
```

`environ` 可注入，便于测试；生产环境默认使用 `os.environ`。

示例错误：

```text
配置文件加载失败: /etc/order-service/config.json
JSON 语法错误: 第 8 行，第 17 列: Expecting ',' delimiter
```

---

### 4. 环境变量覆盖规则

使用双下划线表示嵌套路径：

```text
APP__NAME=payment-service
APP__DEBUG=true
SERVER__PORT=9000
DATABASE__URL=postgresql://db/orders
DATABASE__POOL_SIZE=20
FEATURES__ENABLE_METRICS=false
```

转换规则：

```text
APP__DEBUG       -> {"app": {"debug": "true"}}
SERVER__PORT     -> {"server": {"port": "9000"}}
DATABASE__URL    -> {"database": {"url": "..."}}
```

环境变量名统一：

- 转换为小写后匹配配置字段
- 使用 `__` 分隔嵌套层级
- 单个 `_` 保留在字段名中
- 建议增加统一前缀，例如 `ORDER_SERVICE__`

有前缀时：

```text
ORDER_SERVICE__SERVER__PORT=9000
```

解析结果：

```python
{"server": {"port": "9000"}}
```

不建议自动读取所有环境变量并尝试匹配。应只处理符合前缀的变量，避免系统环境变量意外覆盖配置。

---

### 5. 环境变量类型转换

环境变量本质上都是字符串，应由配置模型统一转换。布尔值必须采用明确规则：

```text
true、1、yes、on  -> True
false、0、no、off -> False
```

大小写不敏感，其他值报错：

```text
环境变量 APP__DEBUG 的布尔值无效: "enabled"
允许的值: true, false, 1, 0, yes, no, on, off
```

建议对特殊类型单独处理：

- `int`：必须是合法整数
- `float`：必须是合法浮点数
- `bool`：使用上述明确值集合
- `list[str]`：使用 JSON 数组，例如 `["a", "b"]`
- `dict[str, str]`：使用 JSON 对象
- `str`：原样保留，不做隐式空字符串转 `None`
- 可选值清空行为必须明确，例如不建议把空字符串自动解释为 `null`

示例：

```text
ALLOWED_ORIGINS=["https://a.example.com","https://b.example.com"]
```

---

### 6. 合并算法

合并必须是递归合并：

```python
def deep_merge(
    base: dict[str, object],
    override: dict[str, object],
) -> dict[str, object]:
    result = dict(base)

    for key, value in override.items():
        old_value = result.get(key)

        if isinstance(old_value, dict) and isinstance(value, dict):
            result[key] = deep_merge(old_value, value)
        else:
            result[key] = value

    return result
```

合并原则：

- 两个值都是对象时递归合并
- 其他类型直接覆盖
- 数组整体替换，不进行按索引合并
- 环境变量只覆盖明确指定的字段
- 未知字段最终由模型拒绝

完整流程：

```python
defaults = {}
json_config = load_json_config(config_path)
env_config = load_environment(environ, prefix="ORDER_SERVICE__")
runtime_config = dict(overrides or {})

merged = deep_merge(defaults, json_config)
merged = deep_merge(merged, env_config)
merged = deep_merge(merged, runtime_config)

return Settings.model_validate(merged)
```

---

### 7. 错误设计

定义统一异常：

```python
class ConfigurationError(Exception):
    """配置加载、解析或校验失败。"""
```

错误应包含：

- 错误来源：JSON、环境变量或运行时参数
- 配置路径：如 `server.port`
- 原始字段名：如 `ORDER_SERVICE__SERVER__PORT`
- 实际值，敏感字段需脱敏
- 期望类型或约束
- 用户可执行的修复建议

示例：

```text
配置校验失败:
- server.port: 输入值 70000 超出允许范围 1..65535
- database.pool_size: 输入值 "large" 不是合法整数
- app.deubg: 未知配置项，可能是拼写错误；允许的字段包括 app.debug
- database.url: 字段必填
```

敏感信息禁止出现在异常、日志和调试输出中：

```text
database.url: 配置值无效
```

不要打印实际连接串、密码、Token 或密钥。

---

### 8. 配置对象的使用方式

应用启动时只加载一次：

```python
settings = load_settings(
    config_path=config_path,
    environ=os.environ,
)
```

业务代码只依赖已校验的 `Settings`：

```python
server_port = settings.server.port
database_url = settings.database.url.get_secret_value()
```

不建议在业务代码中直接读取：

```python
os.getenv("SERVER__PORT")
```

这样可以避免：

- 多处重复解析
- 类型不一致
- 运行过程中配置状态变化
- 错误延迟到业务执行阶段才暴露

配置对象应视为不可变对象。可使用 Pydantic 的冻结配置：

```python
model_config = ConfigDict(
    extra="forbid",
    frozen=True,
)
```

---

### 9. 推荐的模块 API

模块至少提供以下接口：

```python
def load_settings(
    config_path: str | None = None,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
    env_prefix: str = "ORDER_SERVICE__",
) -> Settings:
    """读取并校验最终配置。"""


def load_json_config(path: str) -> dict[str, object]:
    """读取并解析 JSON 配置。"""


def load_environment(
    environ: Mapping[str, str],
    prefix: str,
) -> dict[str, object]:
    """将环境变量转换为嵌套配置对象。"""


def deep_merge(
    base: dict[str, object],
    override: dict[str, object],
) -> dict[str, object]:
    """递归合并两个配置对象。"""
```

内部实现可增加：

```python
def redact_value(path: str, value: object) -> object:
    """对敏感配置进行脱敏。"""


def format_validation_error(error: Exception) -> str:
    """将底层校验错误转换为用户可读信息。"""
```

---

### 10. 验证要求

至少覆盖以下测试场景：

1. 只有默认值时可以加载
2. JSON 正常加载
3. JSON 不存在时返回明确错误
4. JSON 语法错误包含行号和列号
5. JSON 根节点不是对象时失败
6. 环境变量覆盖 JSON
7. 环境变量只覆盖指定字段，不影响其他字段
8. 嵌套字段使用 `__` 正确解析
9. 布尔值支持合法表示
10. 非法布尔值被拒绝
11. 整数、浮点数和范围校验生效
12. 未知 JSON 字段被拒绝
13. 未知环境变量字段被拒绝
14. 数组环境变量使用 JSON 格式解析
15. 运行时参数优先级最高
16. 敏感配置不会出现在异常文本中
17. `environ` 参数可注入，测试不依赖真实系统环境
18. 空字符串与缺失字段行为符合文档
19. 多次调用不会修改输入字典
20. 应用启动时配置校验失败会立即终止启动

最终行为应满足：

```text
默认值 < JSON < 环境变量 < 运行时参数
```

并且所有进入业务代码的配置，都已经经过同一个强类型模型验证。
