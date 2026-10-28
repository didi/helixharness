const assert = require('node:assert/strict')
const test = require('node:test')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { spawnSync } = require('node:child_process')

// 转发路由配置在模块加载时解析并缓存，因此每个用例都用独立子进程 + 临时配置文件，
// 避免相互污染，也顺带覆盖 HELIXHARNESS_FRONTEND_ROUTES_FILE 这条公开覆盖路径。
const LOADER = require.resolve('../lib/load-transponder-routes.cjs')
const SHIPPED_CONFIG = require.resolve('../../config/transponder-routes.json')

function withTempConfig(config, fn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-routes-'))
  const file = path.join(dir, 'routes.json')
  fs.writeFileSync(file, JSON.stringify(config), 'utf8')
  try {
    return fn(file)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
}

/** 在独立子进程里加载配置，返回 { lines, resolved } */
function loadInChild(businessLine, configFile) {
  const script = `
    const { listBusinessLines, resolveTransponderUrlByBusinessLine } =
      require(${JSON.stringify(LOADER)})
    const input = process.argv[1] || ''
    process.stdout.write(JSON.stringify({
      lines: listBusinessLines(),
      resolved: input ? resolveTransponderUrlByBusinessLine(input) : null,
      source: ${JSON.stringify(LOADER)}.replace(/lib\\/load-transponder-routes\\.cjs$/, 'config/transponder-routes.json'),
    }))
  `
  const env = { ...process.env }
  if (configFile) env.HELIXHARNESS_FRONTEND_ROUTES_FILE = configFile
  else delete env.HELIXHARNESS_FRONTEND_ROUTES_FILE

  const result = spawnSync(process.execPath, ['-e', script, businessLine], {
    encoding: 'utf8',
    env,
  })
  assert.strictEqual(result.status, 0, result.stderr)
  return JSON.parse(result.stdout)
}

test('shipped config stays blank so no forwarding service is connected by default', () => {
  const { lines, resolved } = loadInChild('acme')
  assert.deepEqual(lines, [], '随包配置不得预置业务线')
  assert.strictEqual(resolved, null, '未配置转发地址时不得启用转发')

  const shipped = JSON.parse(fs.readFileSync(SHIPPED_CONFIG, 'utf8'))
  assert.deepEqual(shipped.businessLines, [])
  assert.deepEqual(shipped.aliases, {})
  assert.strictEqual(shipped.transponderUrl, '')
})

test('declared business lines share one forwarding URL', () => {
  const config = {
    transponderUrl: 'https://forward.example.com',
    businessLines: ['alpha', 'beta'],
    aliases: { '别名': 'alpha' },
  }
  withTempConfig(config, (file) => {
    const { lines, resolved } = loadInChild('beta', file)
    assert.deepEqual(lines, ['alpha', 'beta'])
    assert.deepEqual(resolved, {
      businessLine: 'beta',
      url: 'https://forward.example.com',
      source: file,
      known: true,
    })
  })
})

test('aliases normalize to canonical business lines', () => {
  const config = {
    transponderUrl: 'https://forward.example.com',
    businessLines: ['alpha'],
    aliases: { '别名': 'alpha' },
  }
  withTempConfig(config, (file) => {
    const { resolved } = loadInChild('别名', file)
    assert.equal(resolved.businessLine, 'alpha')
    assert.equal(resolved.url, 'https://forward.example.com')
    assert.equal(resolved.known, true)
  })
})

test('unknown values pass through and empty values do not enable forwarding', () => {
  const config = {
    transponderUrl: 'https://forward.example.com',
    businessLines: ['alpha'],
    aliases: {},
  }
  withTempConfig(config, (file) => {
    const unknown = loadInChild('futureBusiness', file).resolved
    assert.deepEqual(unknown, {
      businessLine: 'futureBusiness',
      url: 'https://forward.example.com',
      source: file,
      known: false,
    })

    assert.equal(loadInChild('', file).resolved, null)
    assert.equal(loadInChild('   ', file).resolved, null)
  })
})
