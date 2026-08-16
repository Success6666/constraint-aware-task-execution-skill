可以采用“**应用实例级共享客户端 + 依赖注入访问**”的方式：客户端在 FastAPI 应用启动时创建，在应用关闭时释放；业务代码只通过依赖获取它。这样可以复用连接，又不会把客户端做成跨应用、跨测试进程的全局单例。

## 推荐结构

```python
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Request


def build_http_client() -> httpx.AsyncClient:
    timeout = httpx.Timeout(
        connect=2.0,
        read=10.0,
        write=10.0,
        pool=2.0,
    )

    limits = httpx.Limits(
        max_connections=100,
        max_keepalive_connections=20,
        keepalive_expiry=30.0,
    )

    return httpx.AsyncClient(
        base_url="https://api.example.com",
        timeout=timeout,
        limits=limits,
        headers={
            "Accept": "application/json",
            "User-Agent": "my-service/1.0",
        },
        follow_redirects=False,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    client = build_http_client()
    app.state.http_client = client

    try:
        # 可选：启动时执行依赖检查或预热
        # await client.get("/health")
        yield
    finally:
        await client.aclose()


app = FastAPI(lifespan=lifespan)


def get_http_client(request: Request) -> httpx.AsyncClient:
    client = getattr(request.app.state, "http_client", None)
    if client is None:
        raise RuntimeError("HTTP client has not been initialized")
    return client


@app.get("/users/{user_id}")
async def get_user(
    user_id: str,
    client: httpx.AsyncClient = Depends(get_http_client),
):
    response = await client.get(
        f"/users/{user_id}",
        # 特殊请求可以覆盖默认超时
        timeout=httpx.Timeout(connect=1.0, read=5.0, write=5.0, pool=1.0),
    )
    response.raise_for_status()
    return response.json()
```

## 生命周期原则

### 1. 连接复用

`httpx.AsyncClient` 应在应用生命周期内保持打开状态，而不是每个请求创建一次：

```python
# 不推荐
@app.get("/data")
async def data():
    async with httpx.AsyncClient() as client:
        return (await client.get(...)).json()
```

每次创建客户端都会失去连接池和 Keep-Alive 的收益，还会增加 TCP/TLS 建连开销。

应用级客户端可以：

- 复用 TCP 连接；
- 复用 TLS 会话；
- 限制最大连接数；
- 避免请求高峰时无限建立连接。

但不要在请求处理中修改共享客户端的可变状态，例如：

```python
# 不推荐：并发请求之间可能互相覆盖
client.headers["Authorization"] = token
```

应将请求级数据作为参数传入：

```python
await client.get(
    "/profile",
    headers={"Authorization": f"Bearer {token}"},
)
```

### 2. 启动初始化

启动阶段适合创建客户端和加载静态资源，例如：

- HTTP 客户端；
- 数据库连接池；
- 配置解析结果；
- 必要的上游服务健康检查。

如果启动健康检查是强依赖，可以让异常阻止应用启动；如果只是可选预热，则记录告警但继续启动。

如果有多个上游服务，仍然可以按应用实例保存多个客户端：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.clients = {
        "billing": httpx.AsyncClient(base_url="https://billing.example.com"),
        "identity": httpx.AsyncClient(base_url="https://identity.example.com"),
    }

    try:
        yield
    finally:
        await app.state.clients["billing"].aclose()
        await app.state.clients["identity"].aclose()
```

也可以封装成一个 `ClientRegistry`，但不必引入进程级单例。

### 3. 关闭释放

必须在 `finally` 中调用 `aclose()`，以保证：

- 连接池关闭；
- Keep-Alive 连接释放；
- 资源正常回收；
- 测试和开发热重载时不会遗留连接。

不要依赖垃圾回收自动释放网络资源。

多 worker 部署时，每个 worker 都会拥有自己的 FastAPI 应用实例和客户端，这是预期行为。客户端不会跨进程共享连接。

## 超时设计

不要使用无限超时。建议至少分别设置：

```python
httpx.Timeout(
    connect=2.0,  # 建立连接
    read=10.0,    # 等待响应数据
    write=10.0,   # 发送请求体
    pool=2.0,     # 等待连接池空闲连接
)
```

设计时应区分：

- **连接超时**：网络不可达、DNS 或 TLS 建连异常；
- **读取超时**：上游处理过慢；
- **连接池超时**：本服务并发过高，池中没有可用连接；
- **业务总超时**：必要时在服务层使用 `asyncio.timeout()` 约束整个调用链。

例如：

```python
import asyncio

async def fetch_with_deadline(client: httpx.AsyncClient):
    try:
        async with asyncio.timeout(12):
            response = await client.get("/slow-endpoint")
            response.raise_for_status()
            return response.json()
    except TimeoutError:
        # 转换为内部统一的上游超时异常
        raise
```

异常应统一转换，避免把底层 `httpx` 异常直接暴露给 API 消费者：

```python
try:
    response = await client.get("/users/1")
    response.raise_for_status()
except httpx.TimeoutException:
    # 返回 504 或内部错误码
    ...
except httpx.HTTPStatusError:
    # 按上游状态码处理
    ...
except httpx.RequestError:
    # 网络、连接、协议错误
    ...
```

重试应只对幂等请求使用，并限制次数、退避时间和总截止时间。不要在底层客户端中无条件重试所有 POST 请求。

## 测试策略

### 1. 测试生命周期是否正确

使用 `TestClient` 的上下文管理器触发生命周期：

```python
from fastapi.testclient import TestClient

def test_lifespan():
    with TestClient(app) as client:
        assert app.state.http_client is not None
        assert not app.state.http_client.is_closed

    assert app.state.http_client.is_closed
```

不要只写：

```python
client = TestClient(app)
```

因为这样无法明确验证 startup/shutdown 生命周期。

### 2. 使用 `MockTransport` 测试请求逻辑

不要让单元测试访问真实上游：

```python
import httpx


def mock_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/users/123"
    return httpx.Response(
        200,
        json={"id": "123", "name": "Alice"},
        request=request,
    )


def test_get_user(monkeypatch):
    test_client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_handler),
        base_url="https://api.example.com",
    )

    app.dependency_overrides[get_http_client] = lambda: test_client

    try:
        with TestClient(app) as client:
            response = client.get("/users/123")
            assert response.status_code == 200
            assert response.json()["name"] == "Alice"
    finally:
        app.dependency_overrides.clear()
        # 测试替代客户端由测试负责关闭
        import asyncio
        asyncio.run(test_client.aclose())
```

在异步测试中更适合使用 `pytest`、`pytest-asyncio` 或 `anyio`，并直接 `await test_client.aclose()`。

### 3. 测试超时和异常转换

应覆盖：

- 连接超时；
- 读取超时；
- 连接池耗尽；
- 4xx/5xx 响应；
- DNS 或连接失败；
- 上游返回非法 JSON；
- 请求取消或服务关闭时的清理。

例如：

```python
async def timeout_handler(request):
    raise httpx.ReadTimeout("upstream timeout", request=request)
```

### 4. 测试替代方式

依赖覆盖比 monkeypatch 全局变量更适合测试：

```python
app.dependency_overrides[get_http_client] = override_client
```

这样每个测试可以提供独立的 `MockTransport` 或 `AsyncMock`，不会污染其他测试，也能验证应用实例之间互不共享状态。

## 不建议的模式

```python
# 模块级可变单例
client = httpx.AsyncClient()

# 缓存客户端的全局工厂
@lru_cache
def get_client():
    return httpx.AsyncClient()

# 在依赖中每次创建
async def get_client():
    async with httpx.AsyncClient() as client:
        yield client
```

这些模式分别容易造成：

- 生命周期脱离 FastAPI；
- 测试之间共享状态；
- 关闭时机不明确；
- 连接无法有效复用；
- 多个应用实例意外共享客户端。

结论是：使用 `lifespan` 管理客户端，用 `app.state` 保存实例，用依赖注入访问，用请求参数传递动态状态，用明确的分项超时保护调用，并用依赖覆盖和 MockTransport 验证生命周期与异常行为。