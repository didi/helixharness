# 数据库使用规范

> 技术栈：MySQL + MyBatis + HikariCP + PageHelper
> 更新时间：<!-- TODO: 填写日期 -->

## 表设计规范

### 必备字段

所有业务表必须包含以下字段：

```sql
`id`          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '自增主键',
`is_deleted`  TINYINT(1)   NOT NULL DEFAULT 0      COMMENT '是否删除 0-否 1-是',
`create_time` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
`update_time` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
```

- 业务 ID（如 `plan_id`、`strategy_id`）与自增主键 `id` 分离，**禁止**对外暴露 `id`
- 状态字段使用 `TINYINT`，在代码中用枚举映射，注释中列出所有状态值

### 命名规范

- 表名：`小写下划线`，加服务前缀（如 `sc_strategy`、`sc_plan`）
- 字段名：`小写下划线`
- 索引名：唯一索引 `uk_{字段名}`，普通索引 `idx_{字段名}`
- **禁止**使用 MySQL 保留字作为字段名（`status`、`type` 等需确认版本兼容性）

### 字符集

- 统一使用 `utf8mb4`，排序规则 `utf8mb4_general_ci`
- **禁止**使用 `utf3`（无法存储 emoji）

### 约束

- **禁止**使用外键约束（通过应用层维护关联关系）
- **禁止**使用 `FLOAT` / `DOUBLE` 存储金额，使用 `BIGINT`（单位：分）或 `DECIMAL(18,2)`

## 索引规范

- 每张表索引数量 ≤ 5 个（写多读少的表更少）
- 联合索引遵循最左前缀原则，将区分度高的字段放左侧
- 必须有索引的场景：WHERE 条件字段、JOIN ON 字段、ORDER BY 字段（高频查询）
- 超过 500 万行的表，新增索引前评估锁表影响

## 逻辑删除

统一使用 `is_deleted` 软删除，**禁止**物理删除业务数据：

```java
// Mapper 中的删除操作实际是 UPDATE
update sc_xxx set is_deleted = 1, update_time = now() where id = #{id}
```

所有查询条件必须加 `AND is_deleted = 0`，**禁止**漏掉软删除过滤。

## ORM 规范

- 统一使用 MyBatis，**禁止**在代码中写原生 JDBC
- SQL 写在 Mapper XML 中，**禁止**在 Java 代码中拼接 SQL 字符串（SQL 注入风险）
- 参数使用 `#{}` 占位符，**禁止**使用 `${}` 拼接（`${}` 仅用于表名/列名动态场景且需严格校验输入）
- **禁止**在代码中拼接 SQL 字符串（含 JDBC `Statement`、字符串 `+` 拼接等），**必须**使用参数化查询（MyBatis `#{}` / `PreparedStatement`）【安全风险：SQL 注入】
- 禁止 `SELECT *`，必须显式列出所需字段

## 分页

使用 PageHelper 插件，**禁止**手写 LIMIT/OFFSET：

```java
PageHelper.startPage(pageNum, pageSize);
List<XxxDo> result = mapper.selectByCondition(param);
```

## 线上 DDL 变更规范

- **禁止**直接修改线上表名（`RENAME TABLE`）
- **禁止**删除存量字段（`DROP COLUMN`），废弃字段通过代码层面忽略
- 新增字段**必须**有默认值或允许 `NULL`，**禁止**新增 `NOT NULL` 且无默认值的字段

## 查询性能规范

- **禁止**无索引全表扫描。所有 `WHERE` / `JOIN ON` / `ORDER BY` 条件字段必须有对应索引
- 超过 500 万行的表，新增查询前**必须**执行 `EXPLAIN` 验证执行计划，确认无全表扫描
- 覆盖索引优先：高频查询字段尽量使用覆盖索引，减少回表

## 连接池（HikariCP）

```yaml
# 参考配置，按实际并发调整
spring:
  datasource:
    hikari:
      maximum-pool-size: 20
      minimum-idle: 5
      connection-timeout: 3000    # ms
      idle-timeout: 600000        # ms
      max-lifetime: 1800000       # ms
```

> <!-- TODO: 补充本项目实际 HikariCP 配置 -->

## 读写分离

> <!-- TODO: 确认本项目是否有读写分离，有则补充路由规则 -->
