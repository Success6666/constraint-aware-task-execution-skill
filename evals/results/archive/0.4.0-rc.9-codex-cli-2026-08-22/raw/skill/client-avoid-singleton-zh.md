使用 FastAPI 的 `lifespan` 管理一个“每个应用实例一个”的 `httpx.AsyncClient`。它能复用连接池，又不会把客户端做成模块级全局单例。

```python
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request


@dataclass(frozen=True)
class Settings:
    upstream_url: str = "https://api.example.com"
    connect_timeout: float = 2.0
    read_timeout: float = 5.0
    write_timeout: float = 5.0
    pool_timeout: float = 1.0
    max_connections: int = 100
    max_keepalive_connections: int = 20


def create_app(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        timeout = httpx.Timeout(
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.write_timeout,
            pool=settings.pool_timeout,
        )
        limits = httpx.Limits(
            max_connections=settings.max_connections,
            max_keepalive_connections=settings.max_keepalive_connections,
        )

        client = httpx.AsyncClient(
            base_url=settings.upstream_url,
            timeout=timeout,
            limits=limits,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        app.state.upstream_client = client

        try:
            yield
        finally:
            await client.aclose()
            app.state.upstream_client = None

    app = FastAPI(lifespan=lifespan)

    def get_upstream_client(request: Request) -> httpx.AsyncClient:
        client = getattr(request.app.state, "upstream_client", None)
        if client is None:
            raise RuntimeError("HTTP client has not been initialized")
        return client

    @app.get("/users/{user_id}")
    async def get_user(
        user_id: str,
        client: httpx.AsyncClient = Depends(get_upstream_client),
    ):
        try:
            response = await client.get(f"/users/{user_id}")
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Upstream timeout")
        except httpx.HTTPStatusError:
            raise HTTPException(status_code=502, detail="Upstream returned an error")
        except httpx.RequestError:
            raise HTTPException(status_code=502, detail="Upstream unavailable")

    return app
```

### 生命周期原则

- **启动初始化**：在 `lifespan` 中创建客户端，配置连接池、Keep-Alive 和分层超时。
- **连接复用**：同一应用实例内通过依赖注入复用 `AsyncClient`，不要在每个请求中创建客户端。
- **关闭释放**：`finally` 中调用 `await client.aclose()`，确保连接池和后台资源释放。
- **作用域**：`app.state` 属于具体的 FastAPI 应用实例，因此测试应用、主应用、不同配置的应用可以各自拥有客户端。
- **多上游服务**：当不同上游需要不同认证、代理或超时策略时，分别创建多个命名客户端，而不是强行共享一个客户端。

### 超时建议

将超时拆分为：

- `connect`：建立连接的最大时间
- `read`：等待响应数据的最大时间
- `write`：发送请求体的最大时间
- `pool`：等待连接池空闲连接的最大时间

对特殊请求可覆盖默认值：

```python
await client.get("/slow-endpoint", timeout=httpx.Timeout(15.0))
```

通常只对幂等请求做有限重试；重试应放在服务层或专用策略中，并区分超时、连接错误和业务状态码，避免重复提交非幂等请求。

### 测试方式

使用应用工厂和 `MockTransport` 注入假的 HTTP 层：

```python
import httpx
from fastapi.testclient import TestClient


def handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/users/42"
    return httpx.Response(200, json={"id": "42"})


def test_get_user():
    app = create_app(
        Settings(upstream_url="https://test.invalid"),
        transport=httpx.MockTransport(handler),
    )

    with TestClient(app) as client:
        response = client.get("/users/42")

    assert response.status_code == 200
    assert response.json() == {"id": "42"}
```

应至少覆盖：

1. 启动后客户端可用；
2. 多次请求复用同一个客户端；
3. 应用退出时调用 `aclose`；
4. 上游连接、读取和连接池超时；
5. 上游返回 4xx/5xx；
6. `RequestError` 被转换为适当的服务错误；
7. 测试之间客户端和连接池相互隔离。