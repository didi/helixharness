# 兼容性分析参考

只读与改动相关的部分。按“支持环境 → 新增或变化的能力 → 受影响消费者 → 有效缓解 → 剩余风险”检查。版本不明的候选标为待验证；只有支持范围内确实受影响时才认定兼容性缺陷。

## 公共契约与配置（所有语言）

- 参数、返回值、错误码、序列化字段、空值语义和默认值是否影响已有调用方？
- 数据结构、数据库迁移、持久化状态变化是否需要兼容旧数据或新旧版本并存？这里只分析，不执行迁移。
- 依赖、构建目标、导出项或运行配置是否提高环境门槛、移除能力或改变部署前提？
- 定位实际消费者和原有契约。有意且已确认的破坏性变化仍列出迁移影响，不误报为需求偏离。

## JavaScript / TypeScript

适用于 JS、TS、JSX、TSX、Vue、HTML 及前端 / Node 构建配置。

- 从 package.json、锁文件、browserslist、TypeScript、Babel、Vite 或 webpack 配置确认 Node、浏览器、框架及构建产物范围。
- 检查新增语法 / API 在目标运行时中的可用性，分别核对模板、脚本和最终构建链；不默认模板或脚本一定被转译，也不跳过纯脚本的浏览器兼容性。
- 区分语法转译与运行时 API polyfill；核对有效版本守卫、特性检测、fallback 是否覆盖实际调用路径。
- 检查 ESM / CJS、exports、类型声明、框架大版本以及导出删除对下游的影响。

## Objective-C / Objective-C++

适用于 .m、.mm 及相关头文件、Xcode 配置、Podfile。单凭 .h / .hpp 不足以认定是 OC 项目。

- 确认最低系统版本、SDK、架构、静态 / 动态产物和消费者环境。
- 核对新增 API 的可用性及 `@available`、selector 检查是否覆盖调用。
- 检查链接方式、Category 链接选项、framework 切片、符号及 OC / Swift 暴露契约变化。
- 线程、对象生命周期、集合边界等崩溃问题归入代码逻辑影响；区别于平台版本不兼容。

## Java（Android 专项）

先通过 AndroidManifest.xml、Android Gradle 插件或 android 配置确认 Android 项目。普通 Java 服务只使用公共契约检查及其实际 JDK / 依赖约束，不套用 Android API level。

- 确认 minSdk、targetSdk、compileSdk、AGP、Java 版本和 core library desugaring 配置。
- 区分 Android OS API 可用性与 Java 语言 / 类库脱糖；compileSdk 足够不代表低版本运行时可用。
- 检查 `Build.VERSION.SDK_INT` 守卫、替代实现和脱糖对实际路径的覆盖；有效缓解影响最终风险判断。
- 核对提高 minSdk、移除脱糖、公共签名和 JNI 变化对旧设备及 Kotlin / 原生调用方的影响。
