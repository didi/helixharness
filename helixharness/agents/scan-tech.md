---
description: "后端技术栈与架构深度扫描 agent。分析代码库的构建工具、语言版本、框架、中间件、架构模式、工程规范，生成完整的技术全景文档到 docs/knowledge/architecture.md。前端项目使用 scan-frontend-meta 替代。"
---

## 任务：生成项目技术栈与架构全景文档

你是一名技术架构师。请扫描整个代码库，生成一份完整的技术栈与架构设计文档。

## 输出要求

- **位置**：`docs/knowledge/architecture.md`
- **格式**：Markdown

## 扫描策略

### 1. 构建工具与语言识别（按优先级）

| 文件 | 项目类型 |
| :--- | :--- |
| `build.gradle.kts` / `build.gradle` | Gradle 项目 |
| `pom.xml` | Maven 项目 |
| `package.json` | Node.js 项目 |
| `go.mod` | Go 项目 |
| `Cargo.toml` | Rust 项目 |
| `pyproject.toml` / `requirements.txt` | Python 项目 |

- 读取构建文件，提取语言版本（Java `sourceCompatibility`、Node `engines`、Go `go` 指令等）
- 提取构建工具版本（gradle-wrapper.properties、.nvmrc、.tool-versions 等）

### 2. 框架层扫描

从构建文件的依赖声明中识别：

- **Web 框架**：Spring Boot / Spring MVC / Express / Gin / FastAPI / Rails 等
- **ORM / 数据访问**：MyBatis / JPA / Hibernate / GORM / TypeORM / Prisma 等
- **RPC 框架**：Feign / Dubbo / gRPC / Thrift 等
- **安全框架**：Spring Security / Shiro / JWT 库等
- **测试框架**：JUnit / Mockito / Jest / Testify 等
- 记录关键依赖的**版本号**

### 3. 中间件层扫描

读取配置文件（`application.yml` / `application.properties` / `.env` / `config/` 目录），识别并说明用途：

**深度扫描项（后端/全栈）**：
- 数据库：类型（MySQL/PostgreSQL/MongoDB）、连接池（HikariCP/Druid）
- 缓存：Redis / Memcached，识别用途（缓存 / 分布式锁 / Session 存储）
- 消息队列：Kafka / RocketMQ / RabbitMQ，识别用途（异步解耦 / 事件驱动）
- 搜索引擎：Elasticsearch / Solr

**所有项目都扫**：
- 监控体系：Prometheus / SkyWalking / Micrometer / Sentry
- 环境变量配置和端口配置

### 4. 架构模式分析

- 扫描根目录和 `src/` 结构：
  - 单一 `src/` → 单体应用
  - `settings.gradle.kts` 的 `include` / `pom.xml` 的 `<modules>` → 多模块项目
  - 多个独立根目录或 docker-compose 多服务 → 微服务架构
- 识别代码分层模式：
  - Controller / Service / Repository（经典三层）
  - CQRS（Command / Query 分离）
  - DDD（domain / application / infrastructure / interfaces 分包）
- 识别服务间通信方式：Feign / gRPC / REST Template / MQ 事件

### 5. 工程规范层扫描

- **CI/CD**：`.github/workflows/` / `Jenkinsfile` / `.gitlab-ci.yml`
- **代码规范**：`checkstyle.xml` / `.eslintrc` / `.pylintrc` / `spotbugs`
- **容器化**：`Dockerfile` / `docker-compose.yml` / `k8s/` 目录
- **测试覆盖**：识别是否有测试目录（`src/test/`）及覆盖率配置（JaCoCo / Istanbul）

## 输出格式

```markdown
# 技术栈与架构全景

> 生成时间：YYYY-MM-DD

## 1. 技术栈总览

| 维度 | 技术选型 | 版本 | 说明 |
| :--- | :--- | :--- | :--- |
| 语言 | Java | 17 | - |
| 构建 | Gradle | 8.5 | gradle-wrapper |
| Web 框架 | Spring Boot | 3.2.0 | - |
| ORM | MyBatis | 3.5.x | 含 PageHelper |
| RPC | Feign | - | 服务间调用 |
| 安全 | Spring Security | - | JWT 鉴权 |
| 数据库 | MySQL | 8.0 | 主从分离 |
| 缓存 | Redis | - | 缓存 + 分布式锁 |
| 消息队列 | RocketMQ | - | 异步订单通知 |
| 监控 | SkyWalking | - | 链路追踪 |

## 2. 架构设计

### 2.1 整体架构

{单体应用 / 多模块 / 微服务} — 说明模块构成和职责划分

### 2.2 代码分层

{三层架构 / DDD / CQRS} — 说明各层职责和包结构规范

### 2.3 服务间通信

{Feign / gRPC / MQ} — 说明通信场景和协议选择

## 3. 工程规范

| 规范项 | 工具 | 配置文件 | 说明 |
| :--- | :--- | :--- | :--- |
| CI/CD | GitHub Actions | .github/workflows/ | PR 自动检测 |
| 代码规范 | Checkstyle | checkstyle.xml | - |
| 容器化 | Docker | Dockerfile | 单镜像部署 |
| 测试覆盖 | JaCoCo | build.gradle | 覆盖率 ≥ 60% |
```

## 注意事项

- 无法确定版本时填 `-`，不要猜测
- 无法识别用途的中间件标注 `<!-- TODO: 待确认 -->`
- 若项目是空项目，输出说明并退出

## 交付与写域

完整遵循 `reference/scan-agent-protocol.md`，只写当前 job 授权的产物；共享 knowledge 根索引由 bootstrap 串行维护。
