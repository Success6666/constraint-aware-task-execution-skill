下面是一套不依赖容器的 Java 服务 + PostgreSQL 本地开发方案。以下以 Spring Boot、Maven、Java 21、PostgreSQL 16+、Flyway 为例；如果项目使用 Gradle，只需替换 Maven 命令。

## 1. 安装本地依赖

需要安装：

- JDK 21
- Maven 3.9+
- PostgreSQL 16 或更高版本
- Git
- 可选：IntelliJ IDEA、DBeaver 或 `psql`

确认环境：

```bash
java -version
mvn -version
psql --version
```

设置 `JAVA_HOME`：

Linux/macOS：

```bash
export JAVA_HOME=/path/to/jdk-21
export PATH="$JAVA_HOME/bin:$PATH"
```

Windows PowerShell：

```powershell
$env:JAVA_HOME = "C:\Program Files\Java\jdk-21"
$env:Path = "$env:JAVA_HOME\bin;$env:Path"
```

## 2. 初始化 PostgreSQL

启动本机 PostgreSQL 服务。

Linux：

```bash
sudo systemctl enable --now postgresql
```

macOS：

```bash
brew services start postgresql@16
```

Windows：

```powershell
Get-Service postgresql*
Start-Service postgresql-x64-16
```

使用管理员账号连接：

```bash
psql -U postgres
```

创建开发用户和数据库：

```sql
CREATE USER app_dev WITH PASSWORD 'app_dev_password';

CREATE DATABASE app_local
    OWNER app_dev
    ENCODING 'UTF8';

GRANT ALL PRIVILEGES ON DATABASE app_local TO app_dev;

\c app_local

GRANT USAGE, CREATE ON SCHEMA public TO app_dev;
ALTER SCHEMA public OWNER TO app_dev;
```

验证连接：

```bash
psql "postgresql://app_dev:app_dev_password@localhost:5432/app_local"
```

建议为测试单独创建数据库：

```sql
CREATE DATABASE app_test
    OWNER app_dev
    ENCODING 'UTF8';
```

不要在应用配置中使用 `postgres` 超级用户。

## 3. 项目依赖

Maven 依赖至少包括：

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
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-test</artifactId>
        <scope>test</scope>
    </dependency>
</dependencies>
```

如果使用 PostgreSQL 新版本，Flyway 可能还需要数据库专用支持包，按项目实际 Flyway 版本添加对应 PostgreSQL 模块。

## 4. 本地配置

建议通过环境变量注入敏感配置，而不是把密码提交到 Git。

`application.yml`：

```yaml
spring:
  application:
    name: demo-service

  datasource:
    url: ${DB_URL:jdbc:postgresql://localhost:5432/app_local}
    username: ${DB_USERNAME:app_dev}
    password: ${DB_PASSWORD:app_dev_password}
    driver-class-name: org.postgresql.Driver

  jpa:
    open-in-view: false
    hibernate:
      ddl-auto: validate
    properties:
      hibernate:
        format_sql: true

  flyway:
    enabled: true
    locations: classpath:db/migration
    baseline-on-migrate: true

server:
  port: ${SERVER_PORT:8080}

logging:
  level:
    org.flywaydb: INFO
    org.hibernate.SQL: INFO
```

关键点：

- `ddl-auto` 使用 `validate`，由迁移脚本负责建表。
- 不要使用 `create` 或 `create-drop` 作为常规开发配置。
- 数据库 URL、账号、密码都支持环境变量覆盖。

启动前设置变量：

Linux/macOS：

```bash
export DB_URL="jdbc:postgresql://localhost:5432/app_local"
export DB_USERNAME="app_dev"
export DB_PASSWORD="app_dev_password"
```

Windows PowerShell：

```powershell
$env:DB_URL = "jdbc:postgresql://localhost:5432/app_local"
$env:DB_USERNAME = "app_dev"
$env:DB_PASSWORD = "app_dev_password"
```

可额外建立 `application-local.yml`，并通过 `--spring.profiles.active=local` 启用。

## 5. 数据库迁移

目录结构：

```text
src/main/resources/db/migration/
├── V1__create_user_table.sql
├── V2__create_order_table.sql
└── V3__add_user_status.sql
```

示例迁移：

```sql
-- V1__create_user_table.sql
CREATE TABLE app_user (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(100) NOT NULL,
    email VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uk_app_user_username UNIQUE (username),
    CONSTRAINT uk_app_user_email UNIQUE (email)
);

CREATE INDEX idx_app_user_created_at
    ON app_user (created_at);
```

执行迁移：

```bash
mvn flyway:info
mvn flyway:migrate
```

如果由 Spring Boot 自动执行，应用启动时会自动迁移：

```bash
mvn spring-boot:run
```

推荐工作规则：

- 已执行的迁移文件不要修改。
- 新变更使用递增版本号，例如 `V4__...sql`。
- 删除字段、改名等破坏性变更分多步进行。
- 生产环境和本地环境使用同一套迁移历史。
- 需要回滚时新增反向迁移，不直接改旧脚本。

查看迁移状态：

```sql
SELECT installed_rank, version, description, success
FROM flyway_schema_history
ORDER BY installed_rank;
```

## 6. 启动服务

编译：

```bash
mvn clean verify
```

开发启动：

```bash
mvn spring-boot:run
```

或者先打包再启动：

```bash
mvn clean package
java -jar target/demo-service.jar
```

指定本地 profile：

```bash
mvn spring-boot:run -Dspring-boot.run.profiles=local
```

基础健康检查：

```bash
curl http://localhost:8080/actuator/health
```

如果使用 Actuator，添加依赖：

```xml
<dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-actuator</artifactId>
</dependency>
```

并配置：

```yaml
management:
  endpoints:
    web:
      exposure:
        include: health,info
```

## 7. 测试配置

测试数据库建议使用本机 PostgreSQL 的 `app_test`，避免测试污染开发库。

`application-test.yml`：

```yaml
spring:
  datasource:
    url: jdbc:postgresql://localhost:5432/app_test
    username: app_dev
    password: app_dev_password

  jpa:
    hibernate:
      ddl-auto: validate

  flyway:
    clean-disabled: false
```

测试类：

```java
@SpringBootTest
@ActiveProfiles("test")
class UserRepositoryIT {

    @Autowired
    private UserRepository userRepository;

    @Test
    void shouldSaveAndLoadUser() {
        User user = new User();
        user.setUsername("alice");
        user.setEmail("alice@example.com");

        User saved = userRepository.save(user);

        assertThat(saved.getId()).isNotNull();
        assertThat(userRepository.findByUsername("alice")).isPresent();
    }
}
```

运行单元测试：

```bash
mvn test
```

运行集成测试：

```bash
mvn verify
```

如果需要每次测试前重置数据库，可以在测试生命周期中执行：

```sql
TRUNCATE TABLE app_user RESTART IDENTITY CASCADE;
```

不要在测试中直接对开发数据库执行 `flyway clean`。

## 8. 推荐的开发流程

每次开始开发：

```bash
git pull
psql "postgresql://app_dev:app_dev_password@localhost:5432/app_local" -c "SELECT version();"
mvn flyway:info
mvn spring-boot:run
```

新增数据库字段时：

1. 修改实体和业务代码。
2. 添加新的 Flyway migration。
3. 启动服务，让本地数据库执行迁移。
4. 执行单元测试和集成测试。
5. 检查 `flyway_schema_history`。
6. 提交代码和迁移文件。

提交前：

```bash
mvn clean verify
```

## 9. 常见问题

连接被拒绝：

```bash
pg_isready -h localhost -p 5432
```

检查 PostgreSQL 服务是否启动、端口是否正确。

认证失败：

```bash
psql -h localhost -U app_dev -d app_local
```

确认用户名、密码以及 PostgreSQL 的认证配置。

权限不足：

```sql
GRANT ALL PRIVILEGES ON DATABASE app_local TO app_dev;
\c app_local
GRANT USAGE, CREATE ON SCHEMA public TO app_dev;
```

迁移校验失败：

- 检查是否修改过已执行的 `V*__*.sql`。
- 使用 `mvn flyway:info` 查看失败版本。
- 修复数据库或新增迁移，不要简单删除迁移历史。

端口冲突：

```bash
mvn spring-boot:run -Dspring-boot.run.arguments="--server.port=8081"
```

最终的本地依赖关系是：

```text
开发者机器
├── JDK 21
├── Maven
├── Java 服务
└── PostgreSQL
    ├── app_local
    └── app_test
```

这样可以完成数据库初始化、环境配置、服务启动、版本化迁移以及单元测试和集成测试，且所有组件都直接运行在本机。