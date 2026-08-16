当前工作区仅包含技能文件，且处于只读模式，无法创建工程、任务书或提交 Git 变更。下面给出可直接落地的本地开发方案。

**技术基线**

- JDK 21 LTS
- Maven 3.9+，项目提交 Maven Wrapper
- Spring Boot
- PostgreSQL 16+
- Flyway
- Spring Data JPA
- JUnit 5 + Spring Boot Test
- 开发库与测试库严格分离

**项目结构**

```text
.
├─ .mvn/
├─ scripts/
│  ├─ env.example.ps1
│  ├─ check-env.ps1
│  ├─ init-db.ps1
│  ├─ migrate.ps1
│  ├─ start.ps1
│  └─ test.ps1
├─ src/main/java/com/example/service/
├─ src/main/resources/
│  ├─ application.yml
│  └─ db/migration/V1__init.sql
├─ src/test/java/com/example/service/
├─ src/test/resources/application-test.yml
├─ .gitignore
├─ mvnw
├─ mvnw.cmd
├─ pom.xml
└─ README.md
```

`pom.xml` 至少包含：

```xml
<dependency>
  <groupId>org.springframework.boot</groupId>
  <artifactId>spring-boot-starter-web</artifactId>
</dependency>
<dependency>
  <groupId>org.springframework.boot</groupId>
  <artifactId>spring-boot-starter-data-jpa</artifactId>
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
  <groupId>org.postgresql</groupId>
  <artifactId>postgresql</artifactId>
  <scope>runtime</scope>
</dependency>
<dependency>
  <groupId>org.springframework.boot</groupId>
  <artifactId>spring-boot-starter-test</artifactId>
  <scope>test</scope>
</dependency>
```

`application.yml`：

```yaml
spring:
  datasource:
    url: ${DATABASE_URL:jdbc:postgresql://localhost:5432/app_dev}
    username: ${DATABASE_USER:app_user}
    password: ${DATABASE_PASSWORD}
  jpa:
    hibernate:
      ddl-auto: validate
    open-in-view: false
  flyway:
    enabled: true
    locations: classpath:db/migration

server:
  address: 127.0.0.1
  port: ${SERVER_PORT:8080}
```

本地环境文件 `scripts/env.local.ps1` 应加入 `.gitignore`：

```powershell
$env:DATABASE_URL = "jdbc:postgresql://localhost:5432/app_dev"
$env:DATABASE_USER = "app_user"
$env:DATABASE_PASSWORD = "仅用于本机的密码"

$env:TEST_DATABASE_URL = "jdbc:postgresql://localhost:5432/app_test"
$env:TEST_DATABASE_USER = "app_user"
$env:TEST_DATABASE_PASSWORD = $env:DATABASE_PASSWORD
```

数据库初始化逻辑使用 PostgreSQL 管理账号执行：

```sql
SELECT format('CREATE ROLE app_user LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user')
\gexec

SELECT 'CREATE DATABASE app_dev OWNER app_user'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'app_dev')
\gexec

SELECT 'CREATE DATABASE app_test OWNER app_user'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'app_test')
\gexec
```

建议脚本职责如下：

```powershell
# 初始化
.\scripts\init-db.ps1

# 手动迁移
.\mvnw.cmd flyway:migrate `
  "-Dflyway.url=$env:DATABASE_URL" `
  "-Dflyway.user=$env:DATABASE_USER" `
  "-Dflyway.password=$env:DATABASE_PASSWORD"

# 启动
.\mvnw.cmd spring-boot:run

# 测试
$env:DATABASE_URL = $env:TEST_DATABASE_URL
$env:DATABASE_USER = $env:TEST_DATABASE_USER
$env:DATABASE_PASSWORD = $env:TEST_DATABASE_PASSWORD
.\mvnw.cmd clean test
```

迁移文件遵循 `V{版本}__{说明}.sql`，已经提交的迁移不可修改，只能新增版本。Flyway 会维护 `flyway_schema_history` 并按顺序执行迁移，[官方文档](https://documentation.red-gate.com/fd/migrate-277578887.html)。

测试应至少覆盖：

- `@DataJpaTest`：实体映射、约束和查询。
- `@SpringBootTest(webEnvironment = RANDOM_PORT)`：HTTP 到 PostgreSQL 的完整链路。
- 每次测试前迁移测试库。
- 测试数据按事务回滚，禁止连接开发库。
- `ddl-auto=validate`，数据库结构只由 Flyway 管理。

首次运行顺序：

```powershell
Copy-Item scripts\env.example.ps1 scripts\env.local.ps1
. .\scripts\env.local.ps1
.\scripts\check-env.ps1
.\scripts\init-db.ps1
.\scripts\migrate.ps1
.\scripts\test.ps1
.\scripts\start.ps1
```

验收时访问健康接口和业务接口，并检查迁移状态：

```powershell
Invoke-RestMethod http://localhost:8080/actuator/health
psql -U app_user -d app_dev -c "TABLE flyway_schema_history;"
```

PostgreSQL 官方提供 `createdb` 和 `psql` 完成本地数据库管理：[createdb](https://www.postgresql.org/docs/current/app-createdb.html)、[psql](https://www.postgresql.org/docs/current/app-psql.html)。当前唯一未完成项是实际文件落盘与运行验证，需要工作区开放写权限后才能执行。