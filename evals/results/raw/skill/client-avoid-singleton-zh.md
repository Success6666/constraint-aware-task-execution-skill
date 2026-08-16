建议把 HTTP 客户端定义为“应用生命周期级资源”：每个 FastAPI 应用实例创建一个 `httpx.AsyncClient`，所有请求复用它；通过依赖注入传递，不使用模块级单例。

```python
# app.py
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
DEFAULT_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=30.0,
)


def build_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://partner.example.com",
        timeout=DEFAULT_TIMEOUT,
        limits=DEFAULT_LIMITS,
        headers={"User-Agent": "my-service/1.0"},
        follow_redirects=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = build_http_client()
    app.state.http_client = client

    try:
        # 可选：启动时检查配置或执行轻量健康检查
        # await client.get("/health")
        yield
    finally:
        await client.aclose()


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)

    @app.get("/users/{user_id}")
    async def get_user(
        user_id: str,
        client: httpx.AsyncClient = Depends(get_http_client),
    ):
        response = await client.get(f"/users/{user_id}")
        response.raise_for_status()
        return response.json()

    return app


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


app = create_app()
```

生命周期行为：

- **启动初始化**：在 `lifespan` 中创建客户端。若初始化或必要的启动检查失败，应用不会进入可服务状态。
- **连接复用**：`AsyncClient` 内部维护连接池，避免每个请求重新建立 TCP/TLS 连接。
- **关闭释放**：`finally` 中调用 `aclose()`，释放连接池、套接字和后台资源。
- **避免泄漏**：业务代码只通过依赖获取客户端，不在路由中创建 `AsyncClient()`，也不在每次调用后关闭共享客户端。
- **应用实例隔离**：`app.state` 属于具体的 FastAPI 应用实例，测试、不同配置或不同进程之间不会共享隐藏的全局状态。

建议将配置外置：

```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    partner_base_url: str
    connect_timeout: float = 2
    read_timeout: float = 5
    write_timeout: float = 5
    pool_timeout: float = 2
    max_connections: int = 100
    max_keepalive_connections: int = 20
```

超时应分别设置，而不是只设置一个很大的总超时：

- `connect`：建立连接的最长时间；
- `read`：等待响应数据的最长时间；
- `write`：发送请求体的最长时间；
- `pool`：等待连接池空闲连接的最长时间。

超时发生时捕获 `httpx.TimeoutException`，转换为服务自身的错误响应或重试策略。重试应只针对明确幂等的请求，并限制次数、退避时间和总耗时；不要对所有 `POST` 无条件重试。

测试时使用应用工厂和依赖覆盖，不依赖真实网络：

```python
import httpx
import pytest
from fastapi.testclient import TestClient

from app import create_app, get_http_client


@pytest.fixture
def app():
    app = create_app()

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"id": "u-1", "name": "Alice"},
        )
    )
    test_client = httpx.AsyncClient(
        transport=transport,
        base_url="https://test.local",
        timeout=httpx.Timeout(1.0),
    )

    app.dependency_overrides[get_http_client] = lambda: test_client
    return app, test_client


def test_get_user(app):
    application, client = app

    with TestClient(application) as test_client:
        response = test_client.get("/users/u-1")

    assert response.status_code == 200
    assert response.json()["name"] == "Alice"
```

`TestClient` 应使用上下文管理器，以便触发 FastAPI 的启动和关闭生命周期。实际项目中还应补充：

- `MockTransport` 返回 4xx、5xx 和超时异常的测试；
- 验证上游错误是否被正确映射；
- 验证共享客户端被多次请求复用；
- 使用假的客户端记录 `aclose()`，确认关闭阶段释放资源；
- 并发测试连接池上限和池等待超时。

只有在确实需要跨应用实例共享资源时才考虑进程级单例；普通 FastAPI 服务使用 `app.state + lifespan + dependency injection` 已能满足连接复用和资源管理，同时更容易测试和配置。