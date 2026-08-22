## 头像上传服务设计

### 1. 上传流程

```text
客户端
  -> 上传 API
  -> 临时隔离区
  -> 文件真实性校验
  -> 安全扫描
  -> 原图转码/缩略图生成
  -> 对象存储
  -> 元数据数据库
  -> 审计日志
```

上传 API 不直接信任文件名、扩展名、`Content-Type` 或客户端传入的尺寸。

### 2. 文件真实性校验

服务端应：

- 限制请求体大小，例如原图最大 10 MB。
- 将文件写入不可执行的临时目录，并使用随机文件名。
- 读取文件头和完整内容，使用成熟图像库解析，而不是仅检查扩展名。
- 校验：
  - MIME 类型与实际格式一致；
  - 文件结构可被图像库完整解码；
  - 解码后的宽高、颜色空间、帧数符合限制；
  - 文件尾部没有额外拼接内容；
  - 解压后像素数量不超过上限，防止图片解压炸弹；
  - EXIF、ICC、注释等元数据大小受限。
- 对 SVG 默认拒绝。若业务必须支持，应将其视为不可信 XML，禁用脚本、外部实体、外链、事件属性和嵌入资源，并栅格化后保存。
- 对 GIF、PNG、JPEG、WebP 等格式统一转码为服务端生成的 PNG/WebP，避免保留原始主动内容。
- 转码失败、解析异常、格式声明不一致时拒绝。

典型校验逻辑：

```text
extension != allowlist                 -> reject
declared_mime != detected_mime         -> reject
image_decoder.verify(bytes) == false   -> reject
decoded_pixels > MAX_PIXELS             -> reject
animated_frames > MAX_FRAMES           -> reject
transcode(bytes) fails                 -> reject
```

### 3. 拒绝伪装文件和主动内容

重点防护：

- 不依据 `.jpg`、文件名或 `Content-Type` 判断安全性。
- 使用魔数检测 + 完整解码 + 转码三重校验。
- 拒绝 PE、ELF、Mach-O、脚本、HTML、JavaScript、PDF、Office 宏等内容，即使扩展名伪装成图片。
- 对多格式 polyglot 文件进行转码后比对：只存储服务端重新编码的结果，不存储用户原始字节。
- 对可疑文件执行病毒扫描或沙箱检测，扫描结果为恶意、超时或不可用时拒绝。
- 对对象存储设置：
  - 私有 bucket；
  - 禁止执行；
  - 禁止用户控制 `Content-Disposition`；
  - 固定安全的 `Content-Type`；
  - 通过签名 URL 或图片代理读取；
  - 下载响应设置 `X-Content-Type-Options: nosniff` 和严格 CSP。
- 文件名只作为展示字段保存，实际对象键使用不可预测的 UUID。

### 4. 对象存储设计

对象键示例：

```text
avatars/{tenant_id}/{user_id}/{asset_id}/original.webp
avatars/{tenant_id}/{user_id}/{asset_id}/thumb_64.webp
avatars/{tenant_id}/{user_id}/{asset_id}/thumb_256.webp
```

建议：

- 原图和派生图分开存储，均为私有对象。
- 启用服务端加密、版本控制和生命周期策略。
- 设置校验和，如 SHA-256/ETag，防止内容错配。
- 通过 CDN 或图片代理提供访问，不暴露 bucket。
- 上传完成后采用“先对象、后元数据”的状态机，避免数据库指向不存在的对象。

状态示例：

```text
PENDING -> SCANNING -> PROCESSING -> READY
                         |
                         +-> REJECTED
```

### 5. 元数据模型

`avatar_assets`

| 字段 | 说明 |
|---|---|
| `id` | 资产 UUID |
| `user_id` | 用户标识 |
| `tenant_id` | 租户标识 |
| `status` | `PENDING/READY/REJECTED/DELETED` |
| `source_size` | 上传字节数 |
| `source_sha256` | 原始内容哈希 |
| `format` | 服务端确认的格式 |
| `width` / `height` | 解码尺寸 |
| `object_key` | 原图对象键 |
| `mime_type` | 服务端生成的 MIME |
| `scan_result` | 扫描结果 |
| `reject_reason` | 拒绝原因分类 |
| `created_at` / `deleted_at` | 时间字段 |

`avatar_variants`

- `asset_id`
- `variant`：`64`, `128`, `256`
- `object_key`
- `width`, `height`
- `size`
- `sha256`
- `mime_type`

数据库中不保存可执行 HTML，也不把用户输入直接拼入对象 URL。

### 6. 缩略图处理

- 使用受支持的图像处理库，在独立 worker 中执行。
- 先限制解码资源，再执行 EXIF 方向纠正和裁剪。
- 采用中心裁剪或人脸检测裁剪，输出固定尺寸。
- 统一输出无脚本格式，例如 WebP/PNG。
- 删除 EXIF、GPS、设备信息等隐私元数据。
- 缩略图生成失败时资产不可进入 `READY`。
- 对同一内容使用哈希或幂等键，避免重复处理。

### 7. 审计设计

记录以下事件：

- `UPLOAD_STARTED`
- `UPLOAD_ACCEPTED`
- `UPLOAD_REJECTED`
- `MALWARE_DETECTED`
- `TRANSCODE_COMPLETED`
- `THUMBNAIL_CREATED`
- `AVATAR_READ`
- `AVATAR_REPLACED`
- `AVATAR_DELETED`
- `ADMIN_REVIEWED`

每条审计记录至少包含：

```text
event_id, tenant_id, actor_id, target_user_id,
asset_id, action, result, reason_code,
request_id, source_ip, user_agent, timestamp
```

要求：

- 审计日志追加写入、不可由普通应用账号修改。
- 敏感字段脱敏。
- 拒绝原因使用稳定枚举，例如 `INVALID_SIGNATURE`、`DECODE_FAILED`、`ACTIVE_CONTENT`、`MALWARE`。
- 保留策略符合合规要求，并支持按 `request_id` 追踪完整链路。

### 8. API 示例

```http
POST /v1/users/{user_id}/avatar
Content-Type: multipart/form-data
```

响应：

```json
{
  "asset_id": "uuid",
  "status": "PROCESSING"
}
```

查询：

```http
GET /v1/users/{user_id}/avatar
```

服务端只返回签名 URL 或代理 URL，不返回对象存储凭据。

### 9. 测试要求

必须包含：

- 真正 JPEG/PNG/WebP 的正常上传测试；
- 错误扩展名但内容为图片；
- 图片扩展名伪装的 PE、ELF、脚本、HTML、PDF；
- 图片后拼接可执行或 HTML 内容；
- polyglot 文件；
- SVG 脚本、事件属性、外部实体和外链；
- 损坏图片、截断文件、异常 EXIF；
- 超大尺寸、超多帧、压缩炸弹；
- MIME 欺骗和 Unicode/路径穿越文件名；
- 病毒扫描命中、扫描超时、转码失败；
- 对象写入成功但数据库失败，以及反向失败场景；
- 缩略图尺寸、方向、隐私元数据清理；
- 权限测试：用户只能读写自己的头像，管理员操作有审计；
- 幂等、重试、并发替换和删除测试；
- 属性测试或模糊测试验证解析器不会崩溃或绕过校验。

验收标准是：任何进入 `READY` 的对象都必须是服务端成功解码并重新编码的图片；原始上传字节永不直接对外提供。