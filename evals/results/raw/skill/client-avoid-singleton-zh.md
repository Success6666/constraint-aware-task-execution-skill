当前环境是只读沙箱，无法实际写入仓库；项目本身也只有技能文件，没有现有 FastAPI 代码。建议新增以下实现。

`pyproject.toml`：

```toml
[project]
name = "fastapi-http-client-lifecycle"
version = "0.1.0"
dependencies = [
  "fastapi>=0.110",
  "httpx>=0.27",
  "uvicorn>=0.29"
]

[project.optional-dependencies]
test = ["pytest>=8", "pytest-asyncio>=0.23"]
```

`app/main.py`：

```python
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request


def build_client() -> httpx.AsyncClient:
    timeout = httpx.Timeout(
        connect=2.0,
        read=5.0,
        write=5.0,
        pool=2.0,
    )
    limits = httpx.Limits(
        max_connections=100,
        max_keepalive_connections=20,
    )
    return httpx.AsyncClient(
        base_url="https://api.example.com",
        timeout=timeout,
        limits=limits,
    )


def create_app(
    client_factory: Callable[[], httpx.AsyncClient] = build_client,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = client_factory()
        app.state.http_client = client
        try:
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
        try:
            response = await client.get(f"/users/{user_id}")
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise HTTPException(
                status_code=504,
                detail="upstream request timed out",
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail="upstream service returned an error",
            ) from exc
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502,
                detail="upstream service unavailable",
            ) from exc

        return response.json()

    return app


app = create_app()
```

`tests/test_http_client_lifecycle.py`：

```python
import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


class TrackingClient(httpx.AsyncClient):
    def __init__(self, transport):
        super().__init__(transport=transport, base_url="https://upstream.test")
        self.request_count = 0
        self.closed_by_lifespan = False

    async def get(self, *args, **kwargs):
        self.request_count += 1
        return await super().get(*args, **kwargs)

    async def aclose(self):
        self.closed_by_lifespan = True
        await super().aclose()


def test_client_initialized_reused_and_closed():
    def handler(request):
        return httpx.Response(200, json={"id": "42"})

    client = TrackingClient(httpx.MockTransport(handler))
    app = create_app(lambda: client)

    with TestClient(app) as test_client:
        assert test_client.get("/users/42").json() == {"id": "42"}
        assert test_client.get("/users/43").json() == {"id": "42"}
        assert client.request_count == 2
        assert app.state.http_client is client

    assert client.closed_by_lifespan is True


def test_timeout_returns_504():
    def handler(request):
        raise httpx.ReadTimeout("read timeout", request=request)

    app = create_app(
        lambda: TrackingClient(httpx.MockTransport(handler))
    )

    with TestClient(app) as test_client:
        response = test_client.get("/users/42")

    assert response.status_code == 504
    assert response.json()["detail"] == "upstream request timed out"


def test_upstream_http_error_returns_502():
    def handler(request):
        return httpx.Response(503, json={"error": "unavailable"})

    app = create_app(
        lambda: TrackingClient(httpx.MockTransport(handler))
    )

    with TestClient(app) as test_client:
        response = test_client.get("/users/42")

    assert response.status_code == 502
```

这个设计的关键点是：

- `AsyncClient` 只在应用生命周期内创建一次，连接池可复用。
- `lifespan` 负责启动初始化和关闭释放。
- 路由通过 FastAPI 依赖注入取得客户端，没有模块级单例。
- 超时拆分为连接、读取、写入和连接池等待超时。
- `client_factory` 使测试可以注入 `MockTransport`，无需真实网络。
- 非 2xx、网络异常和超时分别转换为稳定的 502/504 响应。

由于文件系统权限限制，无法执行 `pytest` 或提交版本变更。