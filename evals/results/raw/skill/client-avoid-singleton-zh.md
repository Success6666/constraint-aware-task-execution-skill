## 设计结论

- 每个 FastAPI 应用实例创建一个 `httpx.AsyncClient`。
- 在 FastAPI `lifespan` 中初始化和关闭客户端。
- 通过 `request.app.state` 注入，业务请求之间复用连接池。
- 不使用模块级单例；测试和多应用实例可分别注入不同客户端。
- 为连接、读取、写入、连接池等待分别配置超时。
- 测试通过 `httpx.MockTransport` 注入客户端，并验证复用和关闭行为。

## 实现

```python
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Request


@dataclass(frozen=True)
class Settings:
    upstream_base_url: str


def create_upstream_client(settings: Settings) -> httpx.AsyncClient:
    timeout = httpx.Timeout(
        timeout=10.0,  # 总超时
        connect=2.0,
        read=5.0,
        write=5.0,
        pool=2.0,
    )

    limits = httpx.Limits(
        max_connections=100,
        max_keepalive_connections=20,
        keepalive_expiry=30.0,
    )

    return httpx.AsyncClient(
        base_url=settings.upstream_base_url,
        timeout=timeout,
        limits=limits,
        headers={"Accept": "application/json"},
    )


def build_app(
    settings: Settings,
    client_factory: Callable[[Settings], httpx.AsyncClient] = create_upstream_client,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = client_factory(settings)
        app.state.upstream_client = client

        try:
            # 这里可放置其他启动初始化逻辑。
            # 默认不在启动阶段请求上游，避免服务启动依赖上游可用性。
            yield
        finally:
            await client.aclose()

    app = FastAPI(lifespan=lifespan)

    def get_upstream_client(request: Request) -> httpx.AsyncClient:
        return request.app.state.upstream_client

    @app.get("/users/{user_id}")
    async def get_user(user_id: str, request: Request):
        client = get_upstream_client(request)

        response = await client.get(f"/users/{user_id}")
        response.raise_for_status()
        return response.json()

    return app
```

## 关键约束

### 连接复用

业务代码只调用已经创建好的客户端：

```python
response = await client.get("/resource")
```

不要在请求处理函数中执行：

```python
async with httpx.AsyncClient() as client:
    ...
```

后者会导致每次请求重新建立连接，无法有效复用连接池。

### 生命周期

- `lifespan` 启动阶段创建客户端。
- 应用运行期间所有请求共享该客户端。
- `finally` 中调用 `aclose()`，确保连接池、空闲连接和相关资源释放。
- 如果启动阶段后续初始化失败，`finally` 仍会关闭客户端。

### 超时

建议至少分别配置：

- `connect`：建立连接的最长时间。
- `read`：等待响应数据的最长时间。
- `write`：发送请求数据的最长时间。
- `pool`：等待连接池空闲连接的最长时间。
- `timeout`：总超时上限。

特殊请求可以覆盖默认超时：

```python
response = await client.get(
    "/slow-resource",
    timeout=httpx.Timeout(
        timeout=30.0,
        connect=2.0,
        read=20.0,
        write=5.0,
        pool=2.0,
    ),
)
```

不建议在客户端层默认添加重试；重试策略应根据请求是否幂等、错误类型和业务语义单独设计。

## 可选的启动探活

只有在“上游不可用时服务不得启动”的场景，才在 `lifespan` 中进行探活：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    client = create_upstream_client(settings)
    app.state.upstream_client = client

    try:
        await client.get("/health", timeout=3.0)
        yield
    finally:
        await client.aclose()
```

否则建议只初始化客户端，不把外部服务可用性绑定到 FastAPI 启动。

## 测试设计

使用 `MockTransport` 替代真实网络，并通过应用工厂注入客户端：

```python
import httpx
from fastapi.testclient import TestClient


def test_client_is_reused_and_closed():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"id": request.url.path.rsplit("/", 1)[-1]},
        )

    client = httpx.AsyncClient(
        base_url="https://upstream.test",
        transport=httpx.MockTransport(handler),
    )

    created_clients = []

    def factory(settings):
        created_clients.append(client)
        return client

    app = build_app(
        Settings(upstream_base_url="https://upstream.test"),
        client_factory=factory,
    )

    with TestClient(app) as test_client:
        assert test_client.get("/users/1").json() == {"id": "1"}
        assert test_client.get("/users/2").json() == {"id": "2"}

    assert len(created_clients) == 1
    assert calls == 2
    assert client.is_closed
```

应至少覆盖：

1. 应用启动时只创建一个客户端。
2. 多个请求使用同一个客户端。
3. 应用退出后客户端已关闭。
4. 上游返回非 2xx 时，`raise_for_status()` 生效。
5. 超时异常能按接口需要转换为对应的 HTTP 响应。
6. 启动初始化失败时客户端仍会释放。

若存在多个上游服务，按上游分别创建客户端，例如分别配置 `base_url`、认证头、连接池和超时；仍然由同一个应用实例的 `lifespan` 统一管理，而不是为每个请求创建客户端。
