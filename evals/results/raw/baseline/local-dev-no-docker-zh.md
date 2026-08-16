当前无法把方案落地到项目：工作区是空 Git 仓库，但沙箱明确禁止写入，且不允许申请权限提升。创建 `tmp-docs/local-development-plan.md` 已被拒绝，Java、Maven、PostgreSQL 的本机检测命令也被策略拦截，因此不能生成文件、运行迁移、测试或提交版本。

解除只读限制后，将按以下方案直接实现：

- Java 21、Spring Boot 3.5.x、Maven
- 本机安装 PostgreSQL，不使用容器
- 独立的 `app_dev`、`app_test` 数据库和最小权限 `app_user`
- 环境变量管理连接信息，提交 `.env.example`，忽略真实 `.env`
- Flyway 管理 `V1__*.sql` 版本化迁移，禁止 Hibernate 自动建表
- `scripts/check-env.ps1`：检查 JDK、Maven、`psql`、数据库连通性
- `scripts/init-db.ps1`：幂等创建用户、开发库和测试库
- `scripts/start.ps1`：加载本地环境并启动服务
- `scripts/migrate.ps1`：单独执行 Flyway 迁移
- `scripts/test.ps1`：重建测试 schema 后运行真实 PostgreSQL 集成测试
- 提供对应 Unix shell 脚本
- Actuator 健康检查包含数据库状态
- README 覆盖安装、初始化、配置、启动、迁移、测试和常见故障
- `mvn verify` 通过后提交 `v0.1.0`

标准执行入口将是：

```powershell
.\scripts\check-env.ps1
.\scripts\init-db.ps1
Copy-Item .env.example .env
.\scripts\migrate.ps1
.\scripts\start.ps1
.\scripts\test.ps1
```

技术选择基于 [Spring Boot 官方文档](https://docs.spring.io/spring-boot/index.html)、[Spring Data JPA 官方项目](https://github.com/spring-projects/spring-data-jpa) 和 [PostgreSQL createdb 文档](https://www.postgresql.org/docs/current/app-createdb.html)。需要先将当前工作区权限调整为可写，之后才能完成实际交付和验证。