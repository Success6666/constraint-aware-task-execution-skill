建议采用“隔离上传、内容验证、像素级重编码、只发布派生文件”的架构。客户端声明的扩展名和 `Content-Type` 仅用于记录，绝不作为图片判定依据。

## 架构

```text
客户端
  │ 1. 创建上传会话
  ▼
上传 API ── 元数据数据库
  │ 2. 预签名 URL
  ▼
隔离对象存储 quarantine/
  │ 3. 对象创建事件
  ▼
验证与转码 Worker
  ├─ 文件签名、结构、解码和安全检查
  ├─ 完整解码到像素
  ├─ 删除元数据并重新编码
  ├─ 生成头像与缩略图
  └─ 写入审计事件
          │
          ▼
公开/受控对象存储 avatars/
          │
          ▼
        CDN
```

隔离桶禁止公开访问、禁止 CDN 回源，并设置较短生命周期。正式桶只接收 Worker 生成的文件，永远不直接发布用户上传的原文件。

## 上传流程

1. `POST /v1/avatar-uploads`

   创建一次性上传会话，返回随机 `upload_id` 和隔离桶预签名 URL。限制对象大小，例如 10 MB。

2. 客户端上传后调用 `POST /v1/avatar-uploads/{id}/complete`

   服务端通过对象存储接口重新读取对象，不信任客户端提供的哈希、文件名、MIME 或尺寸。

3. Worker 执行安全验证：

   - 允许格式仅为 JPEG、PNG、WebP。
   - 根据真实文件签名和容器结构识别格式。
   - 使用成熟图片解码库完整解码全部帧和像素。
   - 文件必须能被严格解析到合法结束位置，不允许尾随载荷或截断数据。
   - 文件签名、解析结果和解码格式必须一致。
   - 拒绝 SVG、HTML、XML、PDF、脚本、压缩包及其他主动或复合内容。
   - 拒绝 PE、ELF、Mach-O、脚本 shebang 等可执行文件特征。
   - 使用恶意软件扫描作为纵深防御，但不把它当作图片真实性校验的替代品。
   - 限制宽高、总像素数、帧数、解压比例、解码内存和处理时间，防止图片炸弹。
   - 对动画 WebP 可直接拒绝，或只取首帧后重编码；头像服务通常建议拒绝动画。

4. 验证成功后只保留解码得到的像素：

   - 应用 EXIF 方向后删除 EXIF、ICC、XMP、注释和未知块。
   - 转换为固定色彩空间，例如 sRGB。
   - 重新编码为新的 JPEG 或 WebP。
   - 不复制原文件的容器块或元数据。
   - 生成 `64x64`、`128x128`、`256x256`、`512x512` 等缩略图。
   - 设置固定响应头：`Content-Type`、`Content-Length`、`X-Content-Type-Options: nosniff`。
   - 下载响应使用服务端保存的安全文件名，不回显原文件名。

5. 数据库事务更新版本并切换当前头像。旧头像延迟删除，以避免 CDN 缓存和数据库更新之间出现短暂失效。

6. 无论成功或失败，都删除隔离对象；失败记录稳定的错误码，不向客户端暴露解析器内部异常。

完整解码后重新编码是关键边界：即使输入文件在元数据中藏有脚本或二进制载荷，正式桶中也只会出现服务端根据像素重新生成的文件。

## 元数据模型

`avatar_uploads`：

```sql
id                 uuid primary key
user_id            uuid not null
status             varchar not null -- pending/uploaded/processing/ready/rejected/failed
quarantine_key     varchar not null
original_name      varchar
declared_mime      varchar
detected_format    varchar
source_size        bigint
source_sha256      char(64)
width              integer
height             integer
frame_count        integer
rejection_code     varchar
created_at         timestamp
completed_at       timestamp
```

`avatars`：

```sql
id                 uuid primary key
user_id            uuid not null
version             integer not null
source_upload_id    uuid not null
canonical_key       varchar not null
canonical_sha256    char(64) not null
width               integer not null
height              integer not null
format              varchar not null
created_at          timestamp
deleted_at          timestamp
unique (user_id, version)
```

`avatar_variants`：

```sql
avatar_id           uuid not null
variant             varchar not null -- 64, 128, 256, 512
object_key          varchar not null
sha256              char(64) not null
width               integer not null
height              integer not null
byte_size           bigint not null
primary key (avatar_id, variant)
```

对象键使用不可猜测且不可覆盖的版本化路径：

```text
avatars/{user_id}/{avatar_id}/v1/128.webp
```

不要使用原文件名作为对象键，也不要覆盖同一个 CDN URL。

## 审计

审计记录采用追加写入，至少包含：

- `upload_id`、`user_id`、请求 ID、操作者和来源 IP
- 隔离对象版本 ID及 SHA-256
- 声明 MIME、检测格式、尺寸和帧数
- 验证器及转码器版本
- 状态变化、拒绝原因和处理耗时
- 生成文件的对象键、哈希和尺寸
- 头像启用、替换、删除事件

拒绝原因使用稳定枚举，例如：

```text
UNSUPPORTED_FORMAT
CONTENT_TYPE_MISMATCH
EXECUTABLE_CONTENT
ACTIVE_CONTENT
POLYGLOT_OR_TRAILING_DATA
INVALID_IMAGE_STRUCTURE
PIXEL_LIMIT_EXCEEDED
ANIMATION_NOT_ALLOWED
MALWARE_DETECTED
DECODE_TIMEOUT
```

审计日志不能保存图片原始字节、预签名 URL或访问凭证。对高频失败、同一哈希重复上传和恶意文件命中设置监控告警。

## 并发与可靠性

- Worker 以 `upload_id + object_version` 作为幂等键。
- 数据库状态更新使用条件更新，例如仅允许 `uploaded -> processing`。
- 发布顺序为：写完所有派生对象、校验对象哈希、提交数据库、最后切换当前头像。
- 存储失败时不修改现有头像。
- 对象存储启用服务端加密、版本控制和最小权限。
- Worker 运行在无网络出口、非特权、只读根文件系统和受限 CPU/内存环境中。
- 上传接口按用户和 IP 限流，并限制同时处理的任务数。

## 测试方案

核心测试矩阵：

| 场景 | 预期结果 |
|---|---|
| 合法 JPEG、PNG、WebP | 接受并生成全部尺寸 |
| `.jpg` 文件实际为 PE/ELF | `EXECUTABLE_CONTENT` |
| MIME 为 `image/png`，内容为 HTML/脚本 | `ACTIVE_CONTENT` |
| SVG 更名为 PNG | 拒绝 |
| JPEG 后附 ZIP、EXE 或脚本载荷 | `POLYGLOT_OR_TRAILING_DATA` |
| 图片元数据包含 HTML或脚本 | 输出文件中元数据被移除 |
| 异常 ICC、EXIF、PNG chunk | 拒绝或安全重编码，不得崩溃 |
| 超大尺寸、小文件高解压比 | `PIXEL_LIMIT_EXCEEDED` |
| 动画或超多帧文件 | `ANIMATION_NOT_ALLOWED` |
| 截断、校验和错误、畸形容器 | `INVALID_IMAGE_STRUCTURE` |
| 解码超时或内存超限 | 安全终止，原头像不变 |
| 同一完成请求重复提交 | 只生成一个头像版本 |
| 转码中途失败 | 不发布任何不完整版本 |
| 数据库提交失败 | 新对象不可见并由清理任务回收 |
| 未授权读取隔离对象 | 拒绝 |
| CDN 请求伪造扩展名 | 仍返回固定 MIME 和 `nosniff` |

此外应对解码入口做基于真实样本语料的模糊测试，并固定保存每次发现的崩溃样本作为回归用例。验收条件应包括：正式存储中不存在任何用户原始文件，所有可访问头像都能再次被允许列表中的解码器成功解码，并且其哈希与数据库记录一致。