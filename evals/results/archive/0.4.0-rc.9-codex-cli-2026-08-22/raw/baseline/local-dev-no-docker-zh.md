下面是一套不依赖 Docker 的 Java 服务 + PostgreSQL 本地开发方案。以 Spring Boot、Maven、Flyway 为例，适用于 Windows、macOS 和 Linux。

## 1. 固定基础版本

建议团队统一以下版本：

- Java 21 LTS
- Spring Boot 3.x
- Maven 3.9+
- PostgreSQL 16
- Flyway 10+
- Git

检查环境：

```bash
java -version
mvn -version
psql --version
```

## 2. 安装 PostgreSQL

安装 PostgreSQL 原生服务，并确保 `psql` 位于 PATH 中。

建议开发环境使用独立用户和数据库：

```sql
CREATE USER app_dev WITH PASSWORD 'app_dev_password';
CREATE DATABASE app_dev OWNER app_dev;
```

连接测试：

```bash
psql -h localhost -p 5432 -U app_dev -d app_dev
```

推荐约定：

- 主机：`localhost`
- 端口：`5432`
- 数据库：`app_dev`
- 用户：`app_dev`
- 测试数据库：`app_test`

创建测试库：

```sql
CREATE DATABASE app_test OWNER app_dev;
```

不要让应用使用 PostgreSQL 超级用户。

## 3. 项目结构

```text
service/
├─ src/
│  ├─ main/
│  │  ├─ java/com/example/service/
│  │  └─ resources/
│  │     ├─ application.yml
│  │     └─ db/migration/
│  │        └─ V1__init.sql
│  └─ test/
│     ├─ java/com/example/service/
│     └─ resources/
│        └─ application-test.yml
├─ pom.xml
├─ .env.example
└─ README.md
```

## 4. Maven 依赖

核心依赖：

```xml
<dependencies>
    <dependency>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-web</artifactId>
    </dependency>

    <dependency>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-data-jpa</artifactId>
    </dependency>

    <dependency>
        <groupId>org.postgresql</groupId>
        <artifactId>postgresql</artifactId>
        <scope>runtime</scope>
    </dependency>

    <dependency>
        <groupId>org.flywaydb</groupId>
        <artifactId>flyway-core</artifactId>
    </dependency>

    <dependency>
        <groupId>org.flywaydb</groupId>
        <artifactId>flyway-database-postgresql</artifactId>
    </dependency>

    <dependency>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-test</artifactId>
        <scope>test</scope>
    </dependency>
</dependencies>
```

## 5. 配置管理

不要把密码提交到 Git。提交 `.env.example`：

```dotenv
DB_HOST=localhost
DB_PORT=5432
DB_NAME=app_dev
DB_USER=app_dev
DB_PASSWORD=app_dev_password
```

本地实际配置可以通过环境变量提供：

```yaml
spring:
  datasource:
    url: jdbc:postgresql://${DB_HOST:localhost}:${DB_PORT:5432}/${DB_NAME:app_dev}
    username: ${DB_USER:app_dev}
    password: ${DB_PASSWORD:app_dev_password}
    driver-class-name: org.postgresql.Driver

  jpa:
    hibernate:
      ddl-auto: validate
    open-in-view: false
    properties:
      hibernate:
        format_sql: true

  flyway:
    enabled: true
    locations: classpath:db/migration
    baseline-on-migrate: true

server:
  port: ${SERVER_PORT:8080}
```

建议区分配置：

- `application.yml`：通用默认配置
- `application-local.yml`：个人本地覆盖配置
- `application-test.yml`：测试数据库配置
- 生产密码通过环境变量或密钥系统注入

启动本地环境：

```bash
mvn spring-boot:run -Dspring-boot.run.profiles=local
```

或者：

```bash
java -jar target/service.jar --spring.profiles.active=local
```

## 6. 数据库迁移

所有表结构变更都通过 Flyway 管理，不直接修改已执行的迁移文件。

初始迁移示例：

```sql
-- src/main/resources/db/migration/V1__init.sql

CREATE TABLE users (
    id          BIGSERIAL PRIMARY KEY,
    username    VARCHAR(100) NOT NULL UNIQUE,
    email       VARCHAR(255) NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_users_email ON users(email);
```

新增迁移：

```text
V2__add_user_status.sql
V3__create_orders.sql
V4__add_order_user_id.sql
```

启动应用时自动执行迁移：

```bash
mvn spring-boot:run
```

查看 Flyway 状态：

```bash
mvn flyway:info
```

如需独立执行，可在 Maven 中配置 Flyway 插件，并使用：

```bash
mvn flyway:migrate
mvn flyway:validate
```

开发规则：

1. 已提交的迁移文件不可修改。
2. 修复数据库结构必须创建新的版本迁移。
3. 破坏性变更分多步进行：新增字段、回填数据、切换代码、删除旧字段。
4. `ddl-auto` 使用 `validate`，不要使用 `create` 或 `update`。

## 7. 初始化流程

新开发者首次配置：

```text
1. 安装 Java、Maven、PostgreSQL
2. 创建 app_dev 和 app_test 数据库
3. 配置本地环境变量
4. 拉取代码
5. 执行 mvn clean verify
6. 启动应用
7. 访问健康检查接口
```

建议提供初始化脚本或 README 命令：

```bash
mvn clean verify
mvn spring-boot:run -Dspring-boot.run.profiles=local
```

应用启动时由 Flyway 自动创建和升级表结构，因此不需要手工执行 SQL。

## 8. 启动与健康检查

建议引入 Actuator：

```xml
<dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-actuator</artifactId>
</dependency>
```

配置：

```yaml
management:
  endpoints:
    web:
      exposure:
        include: health,info
```

检查：

```bash
curl http://localhost:8080/actuator/health
```

预期：

```json
{"status":"UP"}
```

开发时可以配置数据库连接池参数：

```yaml
spring:
  datasource:
    hikari:
      maximum-pool-size: 10
      minimum-idle: 2
      connection-timeout: 30000
```

## 9. 测试方案

### 单元测试

单元测试不连接数据库：

```bash
mvn test
```

覆盖：

- Service 业务逻辑
- 参数校验
- 异常处理
- DTO 转换
- 权限判断

### 数据库集成测试

因为不使用 Docker，使用本机独立的 `app_test` 数据库：

```yaml
# application-test.yml
spring:
  datasource:
    url: jdbc:postgresql://${DB_HOST:localhost}:5432/${DB_NAME:app_test}
    username: ${DB_USER:app_dev}
    password: ${DB_PASSWORD:app_dev_password}

  flyway:
    clean-disabled: false
```

运行：

```bash
mvn verify -Dspring.profiles.active=test
```

测试隔离方式：

- 每次测试前执行事务回滚，适合单事务测试。
- 使用 `@Sql` 清理数据，适合少量测试。
- 测试套件开始前执行 `flyway clean` 和 `flyway migrate`，适合完整集成测试。
- 不要让测试连接 `app_dev`。

可增加 Maven profile：

```xml
<profiles>
    <profile>
        <id>integration</id>
        <properties>
            <spring.profiles.active>test</spring.profiles.active>
        </properties>
    </profile>
</profiles>
```

执行：

```bash
mvn verify -Pintegration
```

## 10. 常见数据库操作

查看数据库：

```bash
psql -h localhost -U app_dev -d app_dev
```

列出表：

```sql
\dt
```

查看迁移记录：

```sql
SELECT installed_rank, version, description, success
FROM flyway_schema_history
ORDER BY installed_rank;
```

重置本地开发库：

```sql
DROP DATABASE app_dev;
CREATE DATABASE app_dev OWNER app_dev;
```

只对本地开发库执行重置，不要在共享环境或生产环境使用。

## 11. 推荐开发工作流

```text
修改 Java 代码
    ↓
新增或修改测试
    ↓
需要表结构变更时新增 Flyway migration
    ↓
执行 mvn verify
    ↓
启动应用并检查 /actuator/health
    ↓
提交代码和新的迁移文件
```

提交前至少执行：

```bash
mvn clean verify
mvn flyway:validate
```

## 12. 团队约定

- 每位开发者使用自己的本地 PostgreSQL 实例。
- 共享环境只用于联调，不作为个人开发数据库。
- 数据库 schema 由 Flyway 维护。
- 应用不依赖 PostgreSQL 超级用户。
- 密码、令牌和本地覆盖配置不提交到 Git。
- CI 环境使用独立 PostgreSQL 服务；即使 CI 使用其他运行方式，也不影响本地方案。
- README 必须包含安装、初始化、启动、迁移和测试命令。
- 为常见故障提供排查说明：端口占用、认证失败、数据库不存在、迁移失败、Java 版本不匹配。