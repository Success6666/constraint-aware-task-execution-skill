下面给出一个可落地的头像上传服务设计，核心原则是：**文件名和客户端声明的 MIME 类型一律不可信，必须验证真实内容，并只输出经过安全处理的图片。**

## 1. 服务边界

上传服务负责：

- 接收头像文件
- 验证真实文件类型和内容
- 病毒/恶意内容检测
- 写入对象存储
- 生成缩略图
- 保存元数据
- 记录完整审计日志
- 提供查询、替换、删除和访问接口

建议采用异步处理模型：

```text
客户端
  │
  ▼
上传 API
  │ 生成 upload_id，写入隔离区
  ▼
内容验证 Worker
  │
  ├─ 拒绝：记录原因 + 审计
  └─ 通过
       │
       ├─ 解码并重新编码为标准格式
       ├─ 生成缩略图
       ├─ 写入正式对象存储
       └─ 更新元数据为 READY
```

上传完成后只返回 `upload_id`，客户端通过状态接口查询处理结果。

## 2. 上传接口

### `POST /v1/avatars`

使用 `multipart/form-data`，字段：

- `file`：文件内容
- `user_id`：由认证上下文确定，不接受客户端覆盖
- `idempotency_key`：防止重复提交

响应：

```json
{
  "upload_id": "up_01J...",
  "status": "PROCESSING"
}
```

### `GET /v1/avatars/{upload_id}`

```json
{
  "upload_id": "up_01J...",
  "status": "READY",
  "avatar_id": "av_01J...",
  "original": {
    "width": 512,
    "height": 512,
    "format": "jpeg",
    "bytes": 38214
  },
  "variants": {
    "small": "https://cdn.example.com/avatar/.../small.webp",
    "medium": "https://cdn.example.com/avatar/.../medium.webp"
  }
}
```

状态建议：

- `PROCESSING`
- `READY`
- `REJECTED`
- `FAILED`
- `DELETED`

## 3. 真实文件内容校验

校验顺序应尽量在隔离环境内完成。

### 基础限制

- 文件大小上限，例如 10 MB
- 请求体大小上限，防止无限流上传
- 读取超时和处理超时
- 最大像素数，例如 25 MP
- 最大宽高，例如 10,000 × 10,000
- 限制压缩比，防止图片解压炸弹
- 禁止多文件、嵌套压缩包和容器格式

### 类型识别

不要依赖：

- 文件扩展名
- `Content-Type`
- 用户提交的文件名

服务端应：

1. 读取文件头和魔数。
2. 使用成熟的文件类型识别库判断容器格式。
3. 仅允许明确支持的格式，例如 JPEG、PNG、WebP。
4. 要求魔数、解析器识别结果和最终解码结果一致。

建议明确拒绝：

- SVG、HTML、XML
- PDF、Flash、Office 文档
- ZIP、RAR、7z、GZIP
- ELF、PE、Mach-O 等可执行格式
- 脚本、宏、媒体容器及未知格式

### 完整解码验证

对通过魔数检查的文件，使用安全配置的图片解码器：

- 完整解码像素数据，而不是只解析头部
- 拒绝 CRC 错误、截断数据、损坏 chunk、异常颜色空间
- 禁用外部资源加载、网络访问和脚本执行
- 禁止 ImageMagick 等工具的危险 delegate
- 解码后检查实际尺寸、像素数量和内存占用

### 重新编码

不要直接分发用户上传的原始文件：

1. 解码为像素数据。
2. 丢弃元数据、注释、ICC 配置和未知扩展块。
3. 重新编码为内部统一格式，例如 WebP 或 JPEG。
4. 仅把重新编码后的结果写入可访问存储。

这样可消除大量 polyglot、恶意 metadata 和内容注入风险。

### 主动内容和恶意文件

除格式拒绝外，建议接入：

- 恶意软件扫描引擎
- YARA 规则或企业文件安全网关
- 图片解析器 CVE 黑名单和版本更新机制

扫描失败、超时或服务不可用时，文件保持隔离状态，不得发布到 CDN。

## 4. 对象存储设计

使用两个存储区域：

### 隔离区

- 私有 bucket
- 无 CDN、无公开 URL
- 仅验证 Worker 的服务账号可读
- 生命周期自动删除，例如 24 小时
- 服务端加密
- 开启版本控制和访问日志

对象键示例：

```text
quarantine/{tenant_id}/{upload_id}/source
```

### 正式区

只保存服务端重新编码后的对象：

```text
avatars/{tenant_id}/{avatar_id}/original.webp
avatars/{tenant_id}/{avatar_id}/small.webp
avatars/{tenant_id}/{avatar_id}/medium.webp
avatars/{tenant_id}/{avatar_id}/large.webp
```

权限建议：

- 上传服务：可写
- 读取服务/CDN：只读
- 删除服务：最小必要权限
- 禁止客户端直接获得 bucket 写权限
- 通过短期签名 URL 或 CDN 访问

## 5. 缩略图策略

头像通常生成固定变体：

| 变体 | 尺寸 | 用途 |
|---|---:|---|
| `small` | 64 × 64 | 列表、评论 |
| `medium` | 256 × 256 | 个人资料 |
| `large` | 512 × 512 | 详情页 |

处理规则：

- 先按 EXIF 方向旋转，再裁剪为正方形
- 使用中心裁剪或预设人像裁剪策略
- 使用高质量缩放
- 生成 WebP，必要时额外生成 JPEG
- 不能从原始对象直接动态执行不受控转换
- 每个变体记录宽高、字节数、哈希和格式

## 6. 元数据模型

### `avatar_uploads`

```sql
CREATE TABLE avatar_uploads (
  upload_id           VARCHAR(32) PRIMARY KEY,
  tenant_id           VARCHAR(64) NOT NULL,
  user_id             VARCHAR(64) NOT NULL,
  status              VARCHAR(16) NOT NULL,
  quarantine_key      TEXT NOT NULL,
  reject_code         VARCHAR(64),
  reject_message      TEXT,
  source_sha256       CHAR(64),
  detected_format     VARCHAR(16),
  source_bytes        BIGINT,
  source_width        INT,
  source_height       INT,
  created_at          TIMESTAMP NOT NULL,
  processed_at        TIMESTAMP,
  expires_at          TIMESTAMP NOT NULL
);
```

### `avatar_variants`

```sql
CREATE TABLE avatar_variants (
  avatar_id       VARCHAR(32) NOT NULL,
  variant         VARCHAR(16) NOT NULL,
  object_key      TEXT NOT NULL,
  format          VARCHAR(16) NOT NULL,
  width           INT NOT NULL,
  height          INT NOT NULL,
  bytes           BIGINT NOT NULL,
  sha256          CHAR(64) NOT NULL,
  created_at      TIMESTAMP NOT NULL,
  PRIMARY KEY (avatar_id, variant)
);
```

### 约束

- `status = READY` 时必须存在所有必需变体
- `REJECTED` 时不得存在正式区对象
- 同一用户只能有一个当前头像时，用单独的 `user_current_avatars` 表并使用事务更新
- 删除采用软删除 + 对象存储异步清理

## 7. 审计日志

审计记录应不可变，并与业务数据分离。

### `avatar_audit_events`

```sql
CREATE TABLE avatar_audit_events (
  event_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  upload_id      VARCHAR(32) NOT NULL,
  tenant_id      VARCHAR(64) NOT NULL,
  user_id        VARCHAR(64),
  actor_id        VARCHAR(64),
  action         VARCHAR(32) NOT NULL,
  result         VARCHAR(16) NOT NULL,
  reason_code    VARCHAR(64),
  detected_type  VARCHAR(32),
  source_sha256  CHAR(64),
  request_id     VARCHAR(64),
  ip_hash        CHAR(64),
  user_agent     TEXT,
  created_at     TIMESTAMP NOT NULL
);
```

必须记录：

- 上传开始、验证通过、验证拒绝、处理失败
- 替换、删除、恢复
- 操作者、请求 ID、租户和用户
- 拒绝原因，例如 `EXECUTABLE_CONTENT`、`SVG_ACTIVE_CONTENT`、`DECODE_FAILED`
- 哈希、检测格式和扫描结果

审计日志应：

- 追加写入，不允许业务接口修改
- 限制读取权限
- 对敏感字段脱敏
- 支持保留期限和合规导出

## 8. 错误码

对客户端返回稳定、不过度泄露内部细节的错误码：

- `FILE_TOO_LARGE`
- `UNSUPPORTED_FORMAT`
- `INVALID_IMAGE_CONTENT`
- `ACTIVE_CONTENT_NOT_ALLOWED`
- `EXECUTABLE_CONTENT_NOT_ALLOWED`
- `IMAGE_DIMENSIONS_EXCEEDED`
- `MALWARE_DETECTED`
- `PROCESSING_TIMEOUT`

详细解析器错误只写内部日志和审计事件。

## 9. 一致性与可靠性

- 使用 `upload_id` 作为幂等键
- Worker 使用消息队列，支持重试
- 重试必须幂等，避免重复生成对象
- 正式对象写入完成后，再以事务更新数据库为 `READY`
- 数据库显示 `READY` 但对象缺失时，健康检查应自动标记异常并告警
- 隔离区对象设置自动过期
- 记录指标：拒绝率、各拒绝原因、处理延迟、扫描失败率、缩略图失败率

## 10. 测试方案

### 单元测试

- JPEG、PNG、WebP 的有效样本
- 错误魔数和错误扩展名
- `Content-Type` 与真实格式不一致
- 截断文件、损坏 chunk、CRC 错误
- 超大尺寸、超大像素数、压缩炸弹
- 解析器异常和超时
- EXIF 方向处理
- 缩略图尺寸和输出格式
- 幂等键重复提交
- 所有拒绝码映射

### 安全测试

准备专门样本验证拒绝：

- PE/ELF/Mach-O 可执行文件伪装
- HTML、JavaScript、SVG、XML
- 图片与 ZIP/脚本拼接的 polyglot
- 含恶意 payload 的 metadata
- 压缩包伪装图片
- 已知恶意文件测试样本
- 触发外部资源引用的 SVG/XML
- 路径穿越、对象键注入和 Unicode 文件名

### 集成测试

验证完整链路：

1. 上传进入隔离区。
2. 校验失败时不产生正式对象。
3. 校验通过后生成原图和全部缩略图。
4. 数据库状态和对象存储保持一致。
5. 审计事件完整且顺序正确。
6. Worker 重试不会生成重复对象。
7. 删除头像后访问 URL 失效。
8. 扫描服务不可用时不会错误发布文件。

### 属性和模糊测试

对图片解析入口执行：

- 随机字节流
- 变异后的合法图片
- 随机 chunk 顺序和长度
- 超长 metadata
- 边界尺寸和颜色空间

目标是：不崩溃、不越权、不发布未验证对象。

## 11. 最关键的发布门槛

只有同时满足以下条件，状态才能变为 `READY`：

```text
真实格式允许
+ 完整解码成功
+ 尺寸和资源限制通过
+ 恶意内容扫描通过
+ 已重新编码
+ 所有缩略图生成成功
+ 正式对象写入成功
+ 元数据事务提交成功
```

任何一步失败，都只能进入 `REJECTED` 或 `FAILED`，绝不能让隔离区原文件被直接访问。