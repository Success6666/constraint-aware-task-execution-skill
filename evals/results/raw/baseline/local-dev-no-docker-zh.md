我会给出一套可直接落地的约定：采用 Java 21 + Spring Boot + Maven + PostgreSQL 原生安装，使用 Flyway 管理迁移，并用本机 PostgreSQL 的独立测试库完成集成测试。重点会覆盖角色权限、配置优先级、启动顺序、失败行为和验收命令。
**技术基线**

- Java 21
- Spring Boot 3.x
- Maven
- PostgreSQL 16 或 17
- Flyway 负责数据库迁移
- HikariCP 负责连接池
- Spring Boot Actuator 提供健康检查
- 本地 PostgreSQL 原生安装，不使用 Docker
- 本地默认端口：应用 `8080`，PostgreSQL `5432`

**本地 PostgreSQL 安装与启动**

macOS：

```bash
brew install postgresql@16
brew services start postgresql@16
```

Ubuntu/Debian：

```bash
sudo apt install postgresql-16 postgresql-client-16
sudo systemctl enable --now postgresql
```

Windows：

- 安装 PostgreSQL 官方安装包。
- 将 PostgreSQL 服务设置为自动启动。
- 使用 `psql` 或 pgAdmin 执行初始化 SQL。

验证服务：

```bash
pg_isready -h localhost -p 5432
psql --version
```

建议统一设置数据库时区为 UTC：

```sql
ALTER SYSTEM SET timezone = 'UTC';
```

修改后重启 PostgreSQL。

**数据库与角色初始化**

开发环境使用独立数据库和独立应用角色：

```sql
CREATE ROLE app_local LOGIN PASSWORD 'change-me-local';

CREATE DATABASE app_local
  OWNER app_local
  ENCODING 'UTF8'
  TEMPLATE template0;
```

连接到 `app_local` 后执行：

```sql
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION app_local;

ALTER ROLE app_local IN DATABASE app_local
  SET search_path = app, public;

ALTER ROLE app_local IN DATABASE app_local
  SET timezone = 'UTC';

GRANT USAGE, CREATE ON SCHEMA app TO app_local;
```

生产环境建议拆分为两个角色：

- `app_migrator`：拥有建表、修改表结构、执行迁移的权限。
- `app_runtime`：仅拥有业务表的 `SELECT/INSERT/UPDATE/DELETE` 权限。

本地开发可以暂时使用单一角色，但配置结构应保留迁移账号和运行时账号两个概念。

**配置约定**

所有环境差异通过环境变量注入，不把密码硬编码到代码中。

```text
SPRING_PROFILES_ACTIVE=local

DB_HOST=localhost
DB_PORT=5432
DB_NAME=app_local
DB_USER=app_local
DB_PASSWORD=change-me-local
DB_SCHEMA=app

SERVER_PORT=8080
SPRING_JPA_HIBERNATE_DDL_AUTO=validate
```

数据库连接：

```text
jdbc:postgresql://${DB_HOST}:${DB_PORT}/${DB_NAME}?currentSchema=${DB_SCHEMA}&sslmode=disable
```

Spring Boot 关键配置：

```yaml
spring:
  datasource:
    url: jdbc:postgresql://${DB_HOST:localhost}:${DB_PORT:5432}/${DB_NAME:app_local}?currentSchema=${DB_SCHEMA:app}&sslmode=disable
    username: ${DB_USER:app_local}
    password: ${DB_PASSWORD}
    hikari:
      maximum-pool-size: 10
      minimum-idle: 2
      connection-timeout: 3000
      validation-timeout: 1000
      leak-detection-threshold: 10000

  jpa:
    hibernate:
      ddl-auto: validate
    open-in-view: false
    properties:
      hibernate:
        default_schema: ${DB_SCHEMA:app}
        jdbc:
          time_zone: UTC

  flyway:
    enabled: true
    locations: classpath:db/migration
    schemas: ${DB_SCHEMA:app}
    default-schema: ${DB_SCHEMA:app}
    baseline-on-migrate: false
    clean-disabled: true
    validate-on-migrate: true

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

关键原则：

- 禁止 `ddl-auto=create`、`update`。
- Hibernate 只负责校验结构。
- 数据库结构只能通过 Flyway 迁移变更。
- `baseline-on-migrate` 默认为 `false`，避免误把未迁移数据库标记为已完成。
- 本地自动迁移可以开启；生产环境应由独立发布步骤执行迁移。

**迁移设计**

迁移命名规则：

```text
V1__create_users.sql
V2__add_user_status.sql
V3__create_orders.sql
R__refresh_reporting_views.sql
```

规则：

- 已执行的 `V` 迁移禁止修改。
- 迁移必须支持重复执行检查，或使用 PostgreSQL 的 `IF EXISTS/IF NOT EXISTS`。
- 每个迁移完成一个清晰的业务变更。
- 表、索引、约束和初始化数据优先放在同一版本迁移中。
- 不在应用启动代码中执行结构变更。
- 大表变更拆成多个版本，避免长事务阻塞应用。

示例：

```sql
CREATE TABLE app.users (
    id UUID PRIMARY KEY,
    email VARCHAR(320) NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_users_email UNIQUE (email)
);

CREATE INDEX idx_users_status ON app.users(status);
```

首次初始化：

```bash
mvn flyway:migrate
```

检查迁移状态：

```bash
mvn flyway:info
mvn flyway:validate
```

本地重建数据库时，优先删除并重新创建 `app_local`。不要在应用默认配置中开放 `flyway clean`。如需清理，使用显式的本地专用命令，并要求数据库名包含 `_local` 或 `_test`。

**应用启动流程**

推荐顺序：

```text
1. 启动 PostgreSQL
2. 检查数据库连通性
3. 执行 Flyway migrate
4. 执行 Flyway validate
5. 启动 Java 服务
6. 检查健康接口
7. 执行 API 冒烟测试
```

本地启动：

```bash
export DB_PASSWORD=change-me-local
mvn flyway:migrate
mvn spring-boot:run -Dspring-boot.run.profiles=local
```

或者构建后启动：

```bash
mvn clean verify
java -jar target/app.jar --spring.profiles.active=local
```

健康检查：

```bash
curl http://localhost:8080/actuator/health
```

健康检查必须区分：

- `liveness`：进程是否仍在运行。
- `readiness`：数据库和必要依赖是否可用。

数据库不可用时：

- 服务启动失败并返回非零退出码。
- 日志输出主机、端口、数据库名和错误类型。
- 不输出数据库密码。
- 不进行无限重试；最多使用有限次数的指数退避重试。

**Maven 构建与依赖**

核心依赖：

- `spring-boot-starter-web`
- `spring-boot-starter-validation`
- `spring-boot-starter-actuator`
- `spring-boot-starter-jdbc` 或 `spring-boot-starter-data-jpa`
- `org.postgresql:postgresql`
- `org.flywaydb:flyway-core`
- `org.flywaydb:flyway-database-postgresql`
- `spring-boot-starter-test`

插件：

- Spring Boot Maven Plugin
- Maven Compiler Plugin，目标版本为 Java 21
- Surefire 执行单元测试
- Failsafe 执行集成测试
- Flyway Maven Plugin 执行迁移管理

建议定义以下命令：

```bash
mvn test
mvn verify
mvn flyway:validate
mvn flyway:info
mvn flyway:migrate
```

**测试数据库**

不要让测试连接 `app_local`。初始化独立数据库：

```sql
CREATE ROLE app_test LOGIN PASSWORD 'change-me-test';

CREATE DATABASE app_test
  OWNER app_test
  ENCODING 'UTF8'
  TEMPLATE template0;
```

测试配置：

```text
SPRING_PROFILES_ACTIVE=test
DB_NAME=app_test
DB_USER=app_test
DB_PASSWORD=change-me-test
DB_SCHEMA=app
```

测试启动时执行：

```text
Flyway migrate
Hibernate validate
```

测试结束后不自动删除整个数据库，避免并行测试或调试时产生意外数据破坏。数据隔离采用以下方式：

- Repository 集成测试：每个测试使用事务并回滚。
- 非事务 API 测试：每个测试使用唯一测试数据，测试结束清理。
- 迁移测试：使用专用测试数据库，验证从空库完整迁移。
- 并行执行时为每个测试进程使用独立 schema，例如 `test_<run_id>`。

测试至少分为三层：

1. **单元测试**
   - 不依赖 PostgreSQL。
   - 覆盖领域规则、校验、映射和异常处理。

2. **数据库集成测试**
   - 使用本机 PostgreSQL。
   - 验证 SQL、索引、唯一约束、事务和迁移结果。
   - 验证应用启动时 Hibernate `validate` 能通过。

3. **API 集成测试**
   - 启动 Spring Boot 测试上下文。
   - 调用真实 HTTP 接口。
   - 覆盖成功、参数错误、未授权、重复请求和数据库异常。

执行全部检查：

```bash
mvn clean verify
```

**验收标准**

环境完成后，以下条件必须全部满足：

```bash
pg_isready -h localhost -p 5432
mvn flyway:validate
mvn flyway:info
mvn clean verify
curl http://localhost:8080/actuator/health
```

并确认：

- 空数据库可以从零完成全部迁移。
- 重复执行迁移不会产生结构错误。
- 修改已执行迁移会被 `validate` 拒绝。
- 应用不会使用 Hibernate 自动建表。
- 应用无法连接数据库时会快速失败。
- 测试不会写入本地开发数据库。
- 数据库密码不会出现在日志、异常堆栈或提交内容中。
- 所有时间字段统一使用 UTC。
- 本地服务重启后数据仍然存在。
- 删除并重新创建数据库后，迁移可以完整恢复系统。
