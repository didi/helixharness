# 项目类型检测协议

init 命令的 Step 0 执行此脚本，使用 bash 检测项目类型（<100ms），写入状态文件供 Step 1 分支编排。

## 状态文件

`docs/.helixharness-init-state.json`

## 检测脚本

```bash
# 创建 docs 目录（如不存在）
mkdir -p docs

STATE_FILE="docs/.helixharness-init-state.json"

# 默认值（每次都强制重新检测，这一步很快）
PROJECT_TYPE="unknown"
HAS_BACKEND=false
HAS_FRONTEND=false
HAS_BACKEND_BUILD=false
CONFIDENCE=0.5

# 后端快速判定
if [ -f "pom.xml" ] || [ -f "build.gradle" ] || [ -f "build.gradle.kts" ] || [ -f "go.mod" ]; then
  HAS_BACKEND_BUILD=true
  HAS_BACKEND=true
  PROJECT_TYPE="backend"
  CONFIDENCE=0.95
fi

# 前端快速判定 - 支持多种常见前端目录布局
if [ -f "package.json" ]; then
  FRONTEND_FILE_COUNT=$(find src client frontend web apps -maxdepth 5 \( -name "*.tsx" -o -name "*.vue" -o -name "*.jsx" -o -name "*.svelte" \) 2>/dev/null | wc -l)
  HAS_FRONTEND_ENTRY=0
  if [ -f "index.html" ] || [ -f "public/index.html" ]; then
    HAS_FRONTEND_ENTRY=1
  fi
  HAS_BACKEND_DEP=$(grep -E '"(express|koa|fastify|@nestjs/core|nest|@hapi/hapi|hapi|restify|sails|loopback|serverless|aws-sdk|@aws-sdk/[^\"]+)"' package.json 2>/dev/null | wc -l)
  HAS_NODE_CLI=$(grep -E '"bin"[[:space:]]*:|"(commander|yargs|oclif)"' package.json 2>/dev/null | wc -l)
  HAS_FRONTEND_DEP=$(grep -E '"(react|react-dom|vue|@angular/core|svelte|next|nuxt|vite)"' package.json 2>/dev/null | wc -l)
  HAS_FRONTEND_SIGNAL=false
  if [ "$HAS_FRONTEND_DEP" -gt 0 ] || [ "$FRONTEND_FILE_COUNT" -gt 0 ] || [ "$HAS_FRONTEND_ENTRY" -gt 0 ]; then
    HAS_FRONTEND_SIGNAL=true
  fi

  # small/fresh 前端只要有框架依赖、前端文件或 HTML 入口即可识别；没有任何前端信号的歧义
  # package.json 保持 unknown（命令按后端兜底），避免把未列举框架的 Node 服务或 CLI 误扫成前端。
  if [ "$HAS_BACKEND_BUILD" = true ]; then
    if [ "$HAS_FRONTEND_SIGNAL" = true ]; then
      HAS_FRONTEND=true
      PROJECT_TYPE="fullstack-separated"
      CONFIDENCE=0.95
    else
      PROJECT_TYPE="backend"
    fi
  elif [ "$HAS_BACKEND_DEP" -gt 0 ] || [ "$HAS_NODE_CLI" -gt 0 ]; then
    HAS_BACKEND=true
    if [ "$HAS_FRONTEND_SIGNAL" = true ]; then
      HAS_FRONTEND=true
      PROJECT_TYPE="fullstack-node"
    else
      PROJECT_TYPE="backend"
    fi
    CONFIDENCE=0.8
  elif [ "$HAS_FRONTEND_SIGNAL" = true ]; then
    HAS_FRONTEND=true
    PROJECT_TYPE="frontend"
    CONFIDENCE=0.9
  fi
fi

# Monorepo 检测
if [ -f "pnpm-workspace.yaml" ] || [ -f "turbo.json" ] || [ -f "lerna.json" ]; then
  PROJECT_TYPE="monorepo"
  CONFIDENCE=0.95
fi

# 默认 scanPlan（根据项目类型在 Step 1 编排时确定）
SCAN_PLAN="[]"
USER_CONFIRMED="false"

# 全栈/Monorepo 交互确认
if [ "$PROJECT_TYPE" = "fullstack-node" ] || [ "$PROJECT_TYPE" = "fullstack-separated" ] || [ "$PROJECT_TYPE" = "monorepo" ]; then
  echo ""
  echo "🔄 检测到项目含后端框架依赖（${PROJECT_TYPE}）"
  echo ""
  echo "如果 BFF 层较薄，推荐选 1（仅前端扫描）"
  echo ""
  echo "请选择扫描模式："
  echo "  1) 📱 仅前端扫描 - 适合 BFF 很薄的前端系统（推荐）"
  echo "  2) 🔧 仅后端扫描 - 适合以 API 为主的项目"
  echo "  3) 🚀 完整全栈扫描 - 前后端都扫描"
  echo ""
  echo "请输入选项 (1/2/3，默认 1): "
  read -r USER_CHOICE

  case "$USER_CHOICE" in
    2)
      PROJECT_TYPE="backend"
      SCAN_PLAN="[\"scan-tech\",\"scan-db\",\"scan-api\",\"scan-service\",\"scan-boundary\",\"scan-service-meta\"]"
      echo "✅ 已选择：仅后端扫描"
      ;;
    3)
      SCAN_PLAN="[\"scan-tech\",\"scan-db\",\"scan-api\",\"scan-service\",\"scan-boundary\",\"scan-service-meta\",\"scan-frontend-meta\",\"scan-frontend-api\",\"scan-frontend-routes\",\"scan-frontend-state\",\"scan-frontend-components\",\"scan-frontend-tracking\"]"
      echo "✅ 已选择：完整全栈扫描"
      ;;
    *)
      PROJECT_TYPE="frontend"
      SCAN_PLAN="[\"scan-frontend-meta\",\"scan-frontend-api\",\"scan-frontend-routes\",\"scan-frontend-state\",\"scan-frontend-components\",\"scan-frontend-tracking\"]"
      echo "✅ 已选择：仅前端扫描"
      ;;
  esac
  USER_CONFIRMED="true"
fi

# 写入状态文件
cat > "$STATE_FILE" << JSONEOF
{
  "projectType": "$PROJECT_TYPE",
  "detectionLevel": "fast",
  "confidence": $CONFIDENCE,
  "techStack": {
    "hasBackend": $HAS_BACKEND,
    "hasFrontend": $HAS_FRONTEND
  },
  "scanPlan": $SCAN_PLAN,
  "userConfirmed": $USER_CONFIRMED
}
JSONEOF

echo "✅ 项目类型检测完成: $PROJECT_TYPE (confidence: $CONFIDENCE)"
```

## 检测结果

| 项目类型 | 条件 | ScanPlan |
|---------|------|---------|
| `backend` | 有 pom.xml/gradle/go.mod，无 package.json | 6 步后端流程 |
| `frontend` | 有 package.json + 前端依赖、前端组件文件或 HTML 入口，无后端信号；fresh/small repo 也适用 | 6 路前端并行 |
| `fullstack-node` | 有 package.json + Node 后端/CLI 信号，并存在前端依赖、前端文件或 HTML 入口 | 三选一交互菜单 |
| `fullstack-separated` | 有 pom.xml/gradle/go.mod + package.json，并存在前端依赖、前端文件或 HTML 入口 | 三选一交互菜单 |
| `monorepo` | 有 pnpm-workspace/turbo/lerna | 三选一交互菜单 |
| `unknown` | 都不匹配 | 按后端流程兜底 |
