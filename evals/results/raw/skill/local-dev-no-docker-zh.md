# Java + PostgreSQL 本地开发方案（不使用 Docker）

以下方案采用 **Java 21 + Spring Boot 3.x + Maven + PostgreSQL 16+ + Flyway**。数据库和 Java 服务均直接运行在本机。

## 1. 技术约定

- Java：21 LTS
- Web 框架：Spring Boot 3.x
- 构建：Maven Wrapper 或 Maven 3.9+
- 数据访问：Spring Data JPA；复杂 SQL 使用 `JdbcTemplate`
- 数据库：PostgreSQL 16+
- 迁移：Flyway
- 测试：JUnit 5、Spring Boot Test、MockMvc
- 配置：环境变量覆盖默认配置
- 时间：数据库使用 UTC，应用统一使用 `Instant`

建议将数据库 schema 命名为 `app`，应用用户只拥有该 schema 的业务权限，不使用超级用户运行服务。

## 2. 本机安装

安装以下软件：

- JDK 21，并确认：

```bash
java -version
```

- PostgreSQL 16 或更高版本，并确认：

```bash
psql --version
```

- Maven 3.9+（如果项目包含 Maven Wrapper，则优先使用 `./mvnw` 或 `mvnw.cmd`）。

启动本机 PostgreSQL 服务：

```bash
# macOS（Homebrew）
brew services start postgresql@16

# Linux（systemd）
sudo systemctl enable --now postgresql
```

Windows 使用 PostgreSQL 安装程序创建的 PostgreSQL 服务启动即可。

## 3. 初始化数据库

使用 PostgreSQL 管理员账号执行：

```sql
CREATE ROLE app_dev LOGIN PASSWORD 'change-me-in-local-only';
CREATE DATABASE app_dev OWNER app_dev;
```

连接业务数据库后执行：

```sql
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION app_dev;
ALTER ROLE app_dev SET search_path TO app,public;
```

建议本地通过环境变量提供密码，不把真实密码写入版本控制：

```bash
export DB_HOST=localhost
export DB_PORT=5432
export DB_NAME=app_dev
export DB_USER=app_dev
export DB_PASSWORD=change-me-in-local-only
```

验证连接：

```bash
psql "postgresql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}" -c 'select version();'
```

## 4. 应用依赖

加入以下依赖：

- `spring-boot-starter-web`
- `spring-boot-starter-validation`
- `spring-boot-starter-actuator`
- `spring-boot-starter-data-jpa`
- `org.postgresql:postgresql`
- `org.flywaydb:flyway-core`
- `org.flywaydb:flyway-database-postgresql`
- `spring-boot-starter-test`
- `org.testcontainers:junit-jupiter` 不使用，因为本方案不依赖 Docker

数据库驱动和 Flyway 版本应由 Spring Boot 的依赖管理统一控制，避免手工混用不兼容版本。

## 5. 应用配置

默认配置使用环境变量，并提供适合本地开发的安全默认值：

```yaml
spring:
  datasource:
    url: jdbc:postgresql://${DB_HOST:localhost}:${DB_PORT:5432}/${DB_NAME:app_dev}?ApplicationName=java-service
    username: ${DB_USER:app_dev}
    password: ${DB_PASSWORD:change-me-in-local-only}
    hikari:
      maximum-pool-size: 10
      minimum-idle: 2
      connection-timeout: 30000

  jpa:
    open-in-view: false
    hibernate:
      ddl-auto: validate
    properties:
      hibernate:
        default_schema: app
        jdbc:
          time_zone: UTC

  flyway:
    enabled: true
    default-schema: app
    schemas: app
    validate-on-migrate: true
    baseline-on-migrate: true

server:
  port: ${SERVER_PORT:8080}

management:
  endpoints:
    web:
      exposure:
        include: health,info
  endpoint:
    health:
      probes:
        enabled: true
```

关键决策：

- 禁止 Hibernate 自动创建或修改表，使用 `ddl-auto: validate`。
- 所有 schema 变化通过 Flyway 迁移完成。
- 应用启动时自动执行待执行迁移。
- 连接池本地限制为 10，避免占满 PostgreSQL 连接。
- 不在生产配置中保留本地默认密码。

## 6. 数据库迁移规范

使用版本化迁移，命名格式为：

```text
V1__create_user_table.sql
V2__add_user_status.sql
V3__create_order_table.sql
```

示例：

```sql
CREATE TABLE app.users (
    id BIGSERIAL PRIMARY KEY,
    email VARCHAR(320) NOT NULL,
    display_name VARCHAR(100) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uk_users_email UNIQUE (email)
);

CREATE INDEX idx_users_status ON app.users(status);
```

迁移规则：

1. 已执行的迁移不修改，新增迁移修正问题。
2. 每个迁移只完成一个清晰的 schema 变更。
3. 破坏性变更采用扩展—迁移—收缩的多阶段方案。
4. 不在迁移中写入依赖环境的业务数据；必要的固定基础数据使用幂等 SQL。
5. 应用实体字段和数据库字段保持显式映射，不依赖自动命名猜测。

如果数据库是空库，应用第一次启动会自动创建 Flyway 历史表并执行全部迁移。也可以显式执行：

```bash
./mvnw flyway:migrate
```

执行结果检查：

```sql
SELECT installed_rank, version, description, success
FROM app.flyway_schema_history
ORDER BY installed_rank;
```

## 7. 启动流程

### 标准开发启动

```bash
export SPRING_PROFILES_ACTIVE=dev
./mvnw spring-boot:run
```

或先构建再启动：

```bash
./mvnw clean verify
java -jar target/*.jar --spring.profiles.active=dev
```

启动顺序：

1. 读取环境变量和开发配置。
2. 连接 PostgreSQL。
3. 执行 Flyway 校验与迁移。
4. 校验 JPA schema。
5. 启动 HTTP 服务。
6. 通过健康检查确认服务可用。

健康检查：

```bash
curl http://localhost:8080/actuator/health
```

正常结果应包含：

```json
{"status":"UP"}
```

### 常见开发环境变量

```bash
export SERVER_PORT=8080
export DB_HOST=localhost
export DB_PORT=5432
export DB_NAME=app_dev
export DB_USER=app_dev
export DB_PASSWORD=change-me-in-local-only
```

## 8. 测试数据库策略

测试使用本机 PostgreSQL 的独立数据库，不与开发库共享：

```sql
CREATE ROLE app_test LOGIN PASSWORD 'change-me-test-only';
CREATE DATABASE app_test OWNER app_test;
```

测试环境变量：

```bash
export DB_NAME=app_test
export DB_USER=app_test
export DB_PASSWORD=change-me-test-only
export SPRING_PROFILES_ACTIVE=test
```

测试配置原则：

```yaml
spring:
  jpa:
    hibernate:
      ddl-auto: validate
  flyway:
    clean-disabled: false
```

测试启动时由 Flyway 初始化测试数据库。测试不依赖开发数据，测试结束后可清理数据库：

```bash
./mvnw flyway:clean flyway:migrate
```

`flyway:clean` 只能对本地测试数据库使用，不允许用于开发共享库或生产库。

## 9. 测试分层

### 单元测试

- 领域规则、校验器、转换器使用纯 JUnit 5。
- 不启动 Spring，不连接 PostgreSQL。
- 覆盖正常路径、边界值和异常路径。

### Web 层测试

- 使用 `@WebMvcTest`。
- Mock service，验证 HTTP 状态码、请求校验、响应结构和错误格式。

### 数据访问测试

- 使用 `@DataJpaTest` 或针对 repository 的集成测试。
- 连接 `app_test`。
- 使用真实 PostgreSQL 验证约束、索引依赖的查询和事务行为。

### 应用集成测试

- 使用 `@SpringBootTest` 启动完整应用上下文。
- 先执行 Flyway，再验证关键 API 的完整调用链。
- 测试结束后回滚事务或清理测试数据；不能依赖测试执行顺序。

运行全部检查：

```bash
./mvnw clean verify
```

建议构建必须通过：

- 编译
- 单元测试
- 集成测试
- Flyway 校验
- JPA schema 校验
- 静态检查（如项目已启用 Checkstyle、SpotBugs 或类似规则）

## 10. 推荐的开发工作流

1. 启动本机 PostgreSQL。
2. 确认 `app_dev` 可连接。
3. 设置开发环境变量。
4. 新增或修改 Flyway 迁移。
5. 启动应用，让迁移自动执行。
6. 调用健康检查和业务接口。
7. 使用 `app_test` 运行完整测试。
8. 提交代码前执行：

```bash
./mvnw clean verify
```

## 11. 验收标准

方案完成后应满足：

- 新开发者仅安装 JDK、Maven 和 PostgreSQL，并创建本地数据库即可启动。
- 空数据库首次启动能自动完成全部迁移。
- Hibernate 不会自行修改 schema。
- 开发库和测试库完全分离。
- 应用不需要 PostgreSQL 超级用户权限。
- `GET /actuator/health` 返回 `UP`。
- `./mvnw clean verify` 在本机 PostgreSQL 正常运行时通过。
- 删除测试数据库后重新创建，仍能通过迁移和完整测试。
