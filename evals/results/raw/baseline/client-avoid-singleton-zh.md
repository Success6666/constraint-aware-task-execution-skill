**推荐方案**

使用 FastAPI 的 `lifespan` 管理每个应用实例的 `httpx.AsyncClient`。客户端属于应用实例，通过 `app.state` 保存；不使用模块级单例，便于测试、多应用实例和不同配置共存。

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request


def build_http_client(settings) -> httpx.AsyncClient:
    timeout = httpx.Timeout(
        connect=settings.http_connect_timeout,
        read=settings.http_read_timeout,
        write=settings.http_write_timeout,
        pool=settings.http_pool_timeout,
    )

    limits = httpx.Limits(
        max_connections=settings.http_max_connections,
        max_keepalive_connections=settings.http_max_keepalive_connections,
        keepalive_expiry=settings.http_keepalive_expiry,
    )

    return httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=False,
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "application/json",
        },
        trust_env=False,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    client = build_http_client(app.state.settings)
    app.state.http_client = client

    try:
        # 可选：启动时验证关键上游
        if app.state.settings.check_upstream_on_startup:
            response = await client.get(app.state.settings.healthcheck_url)
            response.raise_for_status()

        yield
    finally:
        await client.aclose()


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


app = FastAPI(lifespan=lifespan)
app.state.settings = settings
```

业务代码通过依赖获取客户端：

```python
from typing import Annotated
from fastapi import Depends

HttpClient = Annotated[httpx.AsyncClient, Depends(get_http_client)]


@app.get("/users/{user_id}")
async def get_user(user_id: str, client: HttpClient):
    response = await client.get(
        f"https://user-service.internal/users/{user_id}",
        timeout=httpx.Timeout(
            connect=1.0,
            read=3.0,
            write=3.0,
            pool=0.5,
        ),
    )
    response.raise_for_status()
    return response.json()
```

## 关键设计决策

### 1. 连接复用

每个 FastAPI worker 创建一个 `AsyncClient`，请求之间复用连接池：

- 不要在每个请求中创建 `AsyncClient`。
- 不要在模块导入阶段创建客户端。
- `max_connections` 根据 worker 数量、上游限流和本服务并发量设置。
- `max_keepalive_connections` 控制空闲连接数量。
- `keepalive_expiry` 避免长期保留失效连接。
- 多个完全不同的上游可以：
  - 共用一个客户端，若认证、代理、TLS 和连接策略一致；
  - 分别创建多个客户端，若配置或隔离要求不同。
- 若共用客户端，目标 URL、认证和超时仍应按调用场景显式传递。

### 2. 启动初始化

启动阶段只初始化生命周期内必须存在的资源：

- 创建客户端和连接池配置。
- 可选执行上游健康检查。
- 健康检查失败时让应用启动失败，适用于强依赖上游。
- 对非关键上游不要阻塞启动；首次调用时处理失败或采用后台探活。
- 不要在启动阶段发送业务请求或预热大量连接。

健康检查应有独立、较短的超时：

```python
await client.get(
    settings.healthcheck_url,
    timeout=httpx.Timeout(connect=1, read=2, write=2, pool=0.5),
)
```

### 3. 关闭释放

`lifespan` 的 `finally` 必须执行 `aclose()`：

- 关闭连接池和 keep-alive 连接。
- 确保异常退出和正常退出都释放资源。
- 不要在请求处理函数中关闭共享客户端。
- 流式响应必须在消费完成后关闭响应；使用上下文管理器：

```python
from fastapi.responses import StreamingResponse


async def proxy_stream(client: httpx.AsyncClient, url: str):
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            yield chunk
```

若通过 `StreamingResponse` 返回生成器，应确保生成器结束、取消或异常时都能退出 `async with`。

### 4. 超时

不要使用无限超时。建议分层配置：

```python
httpx.Timeout(
    connect=1.0,  # 建立 TCP/TLS 连接
    read=5.0,    # 等待上游响应数据
    write=5.0,   # 上传请求体
    pool=0.5,    # 等待连接池空闲连接
)
```

原则：

- 客户端默认超时用于兜底。
- 关键接口按业务覆盖 `read` 超时。
- `pool` 超时应较短，用于暴露连接池耗尽。
- 大文件或长轮询接口使用专门的超时配置。
- 超时、连接失败、HTTP 4xx/5xx 分开处理。
- 不要自动重试所有请求；只对明确幂等的请求重试，并限制次数、退避和总耗时。

```python
try:
    response = await client.get(url)
    response.raise_for_status()
except httpx.TimeoutException:
    raise UpstreamTimeout()
except httpx.ConnectError:
    raise UpstreamUnavailable()
except httpx.HTTPStatusError as exc:
    handle_upstream_status(exc.response.status_code)
```

### 5. 配置

将以下参数放入配置对象，而不是散落在业务代码中：

```python
class HttpSettings:
    http_connect_timeout: float = 1.0
    http_read_timeout: float = 5.0
    http_write_timeout: float = 5.0
    http_pool_timeout: float = 0.5
    http_max_connections: int = 100
    http_max_keepalive_connections: int = 20
    http_keepalive_expiry: float = 30.0
    check_upstream_on_startup: bool = False
    user_agent: str = "my-service/1.0"
```

生产环境还应明确：

- 是否使用系统代理，通常服务间调用使用 `trust_env=False`；
- TLS 校验和自定义 CA；
- 上游认证头；
- 日志中禁止记录敏感请求头和完整请求体；
- 上游请求指标：目标、状态码、耗时、超时和异常类型。

## 不建议的方式

```python
# 不推荐：每次请求建立和销毁连接
async with httpx.AsyncClient() as client:
    return await client.get(url)
```

这会失去连接复用，并增加 DNS、TCP 和 TLS 开销。

```python
# 不推荐：模块级共享单例
client = httpx.AsyncClient()
```

模块级客户端难以管理关闭时机，也可能在测试、多事件循环或多应用实例场景中产生资源泄漏。

## 测试策略

### 生命周期测试

验证启动后客户端存在，关闭后客户端已关闭：

```python
from fastapi.testclient import TestClient


def test_client_lifecycle(app):
    with TestClient(app) as client:
        assert app.state.http_client.is_closed is False

    assert app.state.http_client.is_closed is True
```

必须使用 `with TestClient(...)`，否则 lifespan 可能不会执行。

### 不访问真实网络

使用 `httpx.MockTransport` 注入测试客户端：

```python
def mock_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"id": "u-1"},
        request=request,
    )


def build_test_client(app):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_handler),
        timeout=httpx.Timeout(1.0),
    )
    app.state.http_client = client
    return client
```

更推荐让 `lifespan` 接收工厂或配置注入，以便测试替换 `build_http_client`，而不是修改业务依赖。

### 必测行为

至少覆盖：

1. 请求使用同一个应用级客户端。
2. 多次请求能够复用客户端连接池。
3. 启动初始化成功。
4. 关键上游健康检查失败时启动失败。
5. 应用关闭调用 `aclose()`。
6. 连接超时、读取超时、连接池超时分别映射为预期错误。
7. 上游 4xx/5xx 不被误判为网络异常。
8. 上游返回无效 JSON 时有明确错误处理。
9. 流式响应完成、异常和取消时释放响应。
10. 测试完全不依赖真实外部服务。

### 连接复用验证

不要只断言客户端对象相同；可以在集成测试中启动可观测的测试 HTTP 服务，记录连接建立次数，连续发送多个请求，确认连接数符合 keep-alive 预期。同时验证服务关闭后没有未释放连接或 pending task。

## 最终边界

- **应用负责**：创建、持有和关闭客户端。
- **依赖函数负责**：从当前请求的应用实例取得客户端。
- **业务服务负责**：URL、请求参数、单次请求超时和错误映射。
- **配置负责**：连接池、默认超时、TLS、代理和健康检查策略。
- **测试负责**：通过 transport 或工厂注入替代网络实现，并验证完整生命周期。
