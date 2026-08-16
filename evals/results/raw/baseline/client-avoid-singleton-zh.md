推荐采用“应用级客户端 + 依赖注入”：每个 FastAPI 应用实例或 Worker 拥有一个 `httpx.AsyncClient`，由 lifespan 统一创建和关闭。

```python
# app/main.py
from collections.abc import Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request

ClientFactory = Callable[[], httpx.AsyncClient]


def build_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://api.example.com",
        timeout=httpx.Timeout(
            connect=2.0,
            read=10.0,
            write=10.0,
            pool=1.0,
        ),
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=30.0,
        ),
        headers={"User-Agent": "my-fastapi-service/1.0"},
    )


def create_app(
    client_factory: ClientFactory = build_http_client,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = client_factory()
        app.state.http_client = client

        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(lifespan=lifespan)

    def get_http_client(request: Request) -> httpx.AsyncClient:
        return request.app.state.http_client

    @app.get("/resources/{resource_id}")
    async def get_resource(
        resource_id: str,
        client: httpx.AsyncClient = Depends(get_http_client),
    ):
        try:
            response = await client.get(f"/resources/{resource_id}")
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise HTTPException(504, "上游服务响应超时") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, "上游服务请求失败") from exc

        return response.json()

    return app


app = create_app()
```

关键边界：

- `AsyncClient` 在应用启动时创建，进程退出时通过 `aclose()` 释放。
- 所有请求共享同一个连接池，实现 HTTP keep-alive 和连接复用。
- 多 Worker 部署时，每个 Worker 独立持有客户端，不能跨进程或事件循环共享。
- `connect/read/write/pool` 分别设置，避免连接池耗尽时无限等待。
- `base_url` 应由受信配置提供，不接受用户传入完整 URL，防止 SSRF。
- 若启动必须验证上游可用性，可在 `yield` 前执行健康检查；一般不建议因为非关键上游暂时不可用而阻止服务启动。

测试建议使用应用工厂注入 `MockTransport`：

```python
import httpx
from fastapi.testclient import TestClient

from app.main import create_app


def test_client_lifecycle_and_reuse():
    clients: list[httpx.AsyncClient] = []

    def factory() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "42"})

        client = httpx.AsyncClient(
            base_url="https://upstream.test",
            transport=httpx.MockTransport(handler),
        )
        clients.append(client)
        return client

    app = create_app(factory)

    with TestClient(app) as test_client:
        assert test_client.get("/resources/42").status_code == 200
        assert test_client.get("/resources/42").status_code == 200
        assert len(clients) == 1
        assert clients[0] is app.state.http_client
        assert not clients[0].is_closed

    assert clients[0].is_closed


def test_timeout_maps_to_504():
    def factory() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        return httpx.AsyncClient(
            base_url="https://upstream.test",
            transport=httpx.MockTransport(handler),
        )

    with TestClient(create_app(factory)) as client:
        response = client.get("/resources/42")

    assert response.status_code == 504
```

异步测试使用 `httpx.ASGITransport` 时要注意：它本身不会自动触发 FastAPI lifespan，应配合 `asgi-lifespan` 的 `LifespanManager`。

当前工作区只有空 Git 仓库，没有现有 FastAPI 源码，因此本次未修改文件，也没有可执行的项目测试。