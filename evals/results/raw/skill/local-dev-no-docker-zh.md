下面是一套不依赖容器的 Java + PostgreSQL 本地开发方案。假设 Java 服务使用 Spring Boot、Maven 和 Flyway；如果使用 Gradle，命令可替换为对应的 `./gradlew`。

## 1. 固定本地版本

建议团队统一以下版本，避免环境差异：

- JDK 21
- Maven 3.9+
- PostgreSQL 16+
- Git
- IDE：IntelliJ IDEA 或 VS Code

检查环境：

```bash
java -version
mvn -version
psql --version
```

## 2. 安装 PostgreSQL

### Windows

使用 PostgreSQL 官方安装包，安装时记住：

- 超级用户：`postgres`
- 端口：`5432`
- 管理密码：自行设置

安装后确认 PostgreSQL 服务已启动：

```powershell
Get-Service postgresql*
```

### macOS

```bash
brew install postgresql@16
brew services start postgresql@16
```

### Ubuntu/Debian

```bash
sudo apt update
sudo apt install postgresql postgresql-contrib
sudo systemctl enable --now postgresql
```

## 3. 初始化数据库和开发账号

使用 `postgres` 管理员连接：

```bash
psql -U postgres -h localhost
```

执行：

```sql
CREATE USER app_dev WITH PASSWORD 'change_me_local';

CREATE DATABASE app_dev
    OWNER app_dev
    ENCODING 'UTF8';

GRANT ALL PRIVILEGES ON DATABASE app_dev TO app_dev;
```

连接业务库：

```bash
psql -U app_dev -h localhost -d app_dev
```

为默认 schema 授权：

```sql
GRANT USAGE, CREATE ON SCHEMA public TO app_dev;
```

如果项目需要 UUID：

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
```

建议不要让 Java 服务使用 `postgres` 超级用户，而是使用独立的 `app_dev` 账号。

## 4. Java 项目依赖

Spring Boot 项目至少包含：

```xml
<dependencies>
    <dependency>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-web</artifactId>
    </dependency>

    <dependency>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-jdbc</artifactId>
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
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-test</artifactId>
        <scope>test</scope>
    </dependency>
</dependencies>
```

如果使用 JPA，则将 `spring-boot-starter-jdbc` 换成或补充：

```xml
<dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-data-jpa</artifactId>
</dependency>
```

## 5. 配置文件

创建 `application.yml`：

```yaml
spring:
  application:
    name: demo-service

  datasource:
    url: ${DB_URL:jdbc:postgresql://localhost:5432/app_dev}
    username: ${DB_USERNAME:app_dev}
    password: ${DB_PASSWORD:change_me_local}
    driver-class-name: org.postgresql.Driver
    hikari:
      maximum-pool-size: ${DB_POOL_SIZE:10}
      minimum-idle: 2
      connection-timeout: 30000

  flyway:
    enabled: true
    locations: classpath:db/migration
    baseline-on-migrate: true

server:
  port: ${SERVER_PORT:8080}

logging:
  level:
    org.flywaydb: INFO
    org.springframework.jdbc: INFO
```

建议创建 `application-test.yml`：

```yaml
spring:
  datasource:
    url: ${TEST_DB_URL:jdbc:postgresql://localhost:5432/app_dev_test}
    username: ${TEST_DB_USERNAME:app_dev}
    password: ${TEST_DB_PASSWORD:change_me_local}

  flyway:
    clean-disabled: false
```

密码不要提交到 Git。开发机可以通过环境变量覆盖：

```bash
export DB_PASSWORD='真实密码'
```

Windows PowerShell：

```powershell
$env:DB_PASSWORD = "真实密码"
```

## 6. 测试数据库

创建独立测试库，避免测试清理开发数据：

```bash
psql -U postgres -h localhost
```

```sql
CREATE DATABASE app_dev_test
    OWNER app_dev
    ENCODING 'UTF8';

GRANT ALL PRIVILEGES ON DATABASE app_dev_test TO app_dev;
```

然后连接并授权：

```bash
psql -U app_dev -h localhost -d app_dev_test
```

```sql
GRANT USAGE, CREATE ON SCHEMA public TO app_dev;
```

## 7. 数据库迁移

目录结构：

```text
src/main/resources/db/migration/
├── V1__create_user_table.sql
├── V2__create_order_table.sql
└── V3__add_user_status.sql
```

示例 `V1__create_user_table.sql`：

```sql
CREATE TABLE app_user (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(320) NOT NULL UNIQUE,
    display_name VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_app_user_created_at
    ON app_user (created_at);
```

迁移规则：

- 已执行的迁移文件不要修改
- 新增变更使用递增版本号
- 文件名格式为 `V数字__描述.sql`
- 破坏性变更拆成多个迁移步骤
- 生产环境也由 Flyway 执行，不要依赖手工 SQL

启动服务时，Flyway 会自动执行尚未运行的迁移。

也可以使用 Maven 显式执行：

```bash
./mvnw flyway:migrate
```

Windows：

```powershell
mvnw.cmd flyway:migrate
```

如果项目没有 Maven Flyway 插件，直接启动 Spring Boot 即可触发迁移。

## 8. 启动服务

### 直接启动

Linux/macOS：

```bash
./mvnw spring-boot:run
```

Windows：

```powershell
mvnw.cmd spring-boot:run
```

### 打包后启动

```bash
./mvnw clean package
java -jar target/demo-service.jar
```

指定环境变量：

```bash
DB_URL=jdbc:postgresql://localhost:5432/app_dev \
DB_USERNAME=app_dev \
DB_PASSWORD=change_me_local \
SERVER_PORT=8080 \
./mvnw spring-boot:run
```

Windows PowerShell：

```powershell
$env:DB_URL = "jdbc:postgresql://localhost:5432/app_dev"
$env:DB_USERNAME = "app_dev"
$env:DB_PASSWORD = "change_me_local"
$env:SERVER_PORT = "8080"

mvnw.cmd spring-boot:run
```

建议提供健康检查接口，例如：

```text
GET http://localhost:8080/actuator/health
```

对应依赖：

```xml
<dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-actuator</artifactId>
</dependency>
```

## 9. 测试策略

### 单元测试

不连接数据库，覆盖：

- 参数校验
- 业务规则
- 异常处理
- Service 层逻辑

运行：

```bash
./mvnw test
```

### 集成测试

使用 `application-test.yml` 连接 `app_dev_test`：

```java
@SpringBootTest
@ActiveProfiles("test")
class UserRepositoryIT {
}
```

执行：

```bash
./mvnw test -Dspring.profiles.active=test
```

测试开始前可清理测试库：

```bash
mvnw.cmd flyway:clean flyway:migrate
```

只允许对测试库执行 `flyway:clean`，不要在开发库或生产库启用清理。

### API 测试

使用 MockMvc 或 WebTestClient，验证：

- HTTP 状态码
- 请求校验
- JSON 响应结构
- 数据库读写结果
- 重复数据和异常场景

## 10. 推荐日常流程

```text
启动 PostgreSQL
    ↓
确认 app_dev / app_dev_test 存在
    ↓
拉取代码
    ↓
配置 DB_PASSWORD
    ↓
启动 Java 服务
    ↓
Flyway 自动执行迁移
    ↓
运行单元测试和集成测试
```

常用命令：

```bash
# 查看数据库
psql -U app_dev -h localhost -d app_dev

# 查看迁移状态
./mvnw flyway:info

# 执行迁移
./mvnw flyway:migrate

# 运行测试
./mvnw test

# 启动服务
./mvnw spring-boot:run
```

## 11. 团队约定

建议将以下内容提交到仓库：

```text
.mvn/
mvnw
mvnw.cmd
pom.xml
src/main/resources/application.yml
src/main/resources/db/migration/
src/test/resources/application-test.yml
.env.example
README.md
```

`.env.example`：

```dotenv
DB_URL=jdbc:postgresql://localhost:5432/app_dev
DB_USERNAME=app_dev
DB_PASSWORD=change_me_local
TEST_DB_URL=jdbc:postgresql://localhost:5432/app_dev_test
TEST_DB_USERNAME=app_dev
TEST_DB_PASSWORD=change_me_local
SERVER_PORT=8080
```

不要提交：

```text
.env
真实数据库密码
本地数据库数据目录
target/
.idea/
```

这样每位开发者只需安装 JDK、Maven 和 PostgreSQL，初始化两个数据库后即可完成迁移、启动和测试。