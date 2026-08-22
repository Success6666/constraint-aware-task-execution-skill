建议采用“应用生命周期内单实例、请求级依赖注入”的方式：客户端不是全局单例，而是由 FastAPI 应用创建并管理，在整个进程生命周期内复用连接。

```python
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Request

DEFAULT_TIMEOUT = httpx.Timeout(
    connect=2.0,
    read=5.0,
    write=5.0,
    pool=2.0,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    client = httpx.AsyncClient(
        base_url="https://api.example.com",
        timeout=DEFAULT_TIMEOUT,
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=30.0,
        ),
        headers={"User-Agent": "my-service/1.0"},
    )

    app.state.http_client = client

    try:
        # 可选：启动时执行健康检查或加载远端配置
        # await client.get("/health")
        yield
    finally:
        await client.aclose()

app = FastAPI(lifespan=lifespan)

def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client

@app.get("/users/{user_id}")
async def get_user(
    user_id: str,
    client: httpx.AsyncClient = Depends(get_http_client),
):
    response = await client.get(f"/users/{user_id}")
    response.raise_for_status()
    return response.json()
```

### 生命周期原则

- **连接复用**：禁止在每个请求中创建 `AsyncClient`。复用同一个应用级客户端，使连接池和 keep-alive 生效。
- **启动初始化**：在 `lifespan` 中创建客户端；必要时进行健康检查、认证令牌预加载或远端配置初始化。
- **关闭释放**：在 `finally` 中调用 `aclose()`，确保连接池、底层 socket 和后台资源释放。
- **避免单例**：不要使用模块级 `client = AsyncClient()` 或自定义 Singleton。把客户端放在 `app.state`，通过 `Request` 依赖取得，便于测试、多应用实例和不同配置并存。
- **多上游服务**：为每个上游封装一个小型 client 类，但仍由 lifespan 创建并注入；不要让业务代码直接依赖全局变量。

```python
class BillingClient:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def charge(self, user_id: str) -> dict:
        response = await self.http.post("/charges", json={"user_id": user_id})
        response.raise_for_status()
        return response.json()
```

### 超时与错误处理

应设置明确的分项超时，而不是无限等待：

```python
httpx.Timeout(connect=2, read=5, write=5, pool=2)
```

建议：

- 对连接、读取、写入、连接池分别设置上限。
- 区分 `ConnectTimeout`、`ReadTimeout`、`PoolTimeout` 和 `HTTPStatusError`。
- 只对幂等请求或明确安全的操作重试。
- 使用指数退避，并限制最大重试次数。
- 外部错误转换为稳定的服务错误，不直接泄露上游响应细节。
- 配合熔断、并发限制和日志中的 request ID，避免上游故障拖垮服务。

### 测试设计

测试时覆盖生命周期和依赖替换，不连接真实上游：

```python
import httpx
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def client():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"id": "u1"},
        )
    )

    with TestClient(app) as test_client:
        app.state.http_client = httpx.AsyncClient(transport=transport)
        yield test_client

        # 实际项目中应在 fixture teardown 中显式关闭替代客户端
```

更推荐将客户端工厂抽出来，测试时直接覆盖依赖：

```python
app.dependency_overrides[get_http_client] = lambda: mock_http_client
```

至少验证以下场景：

1. 多次请求复用同一客户端，而不是重复创建连接池。
2. 应用退出后客户端已调用 `aclose()`。
3. 连接超时、读取超时、池耗尽时返回预期错误。
4. 上游 4xx/5xx 的映射行为。
5. 测试结束后 mock client 和 transport 没有资源泄漏。
6. 并发请求下连接池上限生效。

如果服务需要在同一进程中运行多个 FastAPI app，或需要为不同租户使用不同上游配置，应为每个应用或租户上下文创建独立客户端；这正是生命周期依赖优于全局单例的地方。