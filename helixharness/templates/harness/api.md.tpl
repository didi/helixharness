# API 接口设计规范

> 技术栈：Spring Boot + Spring MVC + Hibernate Validator + Swagger
> 更新时间：<!-- TODO: 填写日期 -->

## 统一响应格式

沿用项目已有、经确认的响应协议；从实际响应类型、接口定义和测试中记录包装类型（如有）、字段名、字段类型、成功/失败判定以及空数据的表示方式。不得为套用模板新增响应包装类或改变已有接口契约。

- 有统一包装时，使用项目实际类型和字段，不预设类名或成功码。
- 没有统一包装时，保留项目已有返回方式，明确 HTTP 状态、响应体和错误的关系。
- 新项目或证据不足时，将待决定的响应格式与成功/失败语义标为 `<!-- TODO: 待确认 -->`，确认后补充与实际协议一致的成功、失败示例。

## 错误码规范

沿用项目已有的错误定义、管理位置与编码规则。错误可由项目的枚举、常量、异常类型或协议文件统一管理，不预设枚举名称，也不要求新增一套错误码。

- 列出真实定义位置、成功/失败标识、常见错误及客户端处理方式。
- 若项目使用业务错误码，记录其实际类型、格式及与 HTTP 状态的映射；新增错误遵循同一规则，避免散落未定义的魔法值。
- 缺少既定规则时标为 `<!-- TODO: 待确认 -->`，不得把示例名称或数值写成项目事实。

## 入参校验

- 使用 Hibernate Validator 注解：`@NotNull`、`@NotBlank`、`@Size`、`@Pattern` 等
- Controller 方法参数必须加 `@Valid` / `@Validated`
- 校验失败由 `@RestControllerAdvice` 统一处理，**禁止**在 Service 层手动判断格式合法性

```java
@RestControllerAdvice
public class GlobalExceptionHandler {
    @ExceptionHandler(MethodArgumentNotValidException.class)
    public /* 项目实际响应类型 */ Object handleValidException(MethodArgumentNotValidException ex) {
        // 按项目实际响应协议和参数校验错误定义返回结果
    }
}
```

## 幂等性

所有**写操作**（创建/修改/删除）必须保证幂等：

- 客户端传入业务唯一 ID（`requestId` / `bizId`）
- 服务端用 Redis Key（`RedisKeys.idempotenceKey(requestId)`）做幂等检查
- TTL 根据业务幂等窗口设置

> <!-- TODO: 确认本项目幂等实现方案（Redis 分布式锁 / 数据库唯一索引 / 版本号） -->

## 接口文档

每个 Controller 及方法必须加 Swagger 注解：

```java
@Api(tags = "xxx 管理")
@RestController
public class XxxController {

    @ApiOperation("创建 xxx")
    @PostMapping("/api/v1/xxx")
    public /* 项目实际响应类型 */ Object create(@Valid @RequestBody XxxCreateRequest req) { ... }
}
```

DTO 字段必须加 `@ApiModelProperty`，**标注是否必填、取值范围**。

## HTTP 客户端规范

所有对外 HTTP 调用必须设置超时，**禁止**使用默认无超时配置：

```java
// connectTimeout ≤ 1000ms，readTimeout ≤ 3000ms
// TODO: 确认本项目各下游服务的超时配置
```

## 版本管理

- URL 中包含版本号：`/api/v1/xxx`
- 已上线接口不能直接修改/删除入参字段，只能**新增可选字段**或发布新版本
- 废弃字段用 `@Deprecated` + `@ApiModelProperty(hidden = true)` 标注，至少保留一个版本

## 分页规范

使用 PageHelper，**禁止**在 SQL 中手写 LIMIT/OFFSET：

```java
PageHelper.startPage(pageNum, pageSize);
List<XxxDo> list = mapper.selectByCondition(condition);
PageInfo<XxxDo> pageInfo = new PageInfo<>(list);
```

- 默认 pageSize ≤ 100，超出拒绝或 cap 到 100
