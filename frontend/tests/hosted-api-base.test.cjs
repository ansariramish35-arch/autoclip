// Hosted builds must reach a backend on another origin without waiting for the
// desktop backend-ready event, which never fires in a browser.
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const { runInSandbox } = require('./vm-sandbox.cjs')

const SOURCE = fs.readFileSync(path.join(__dirname, '../src/utils/apiConfig.ts'), 'utf8')
// vite.config.ts injects __AUTOCLIP_API_BASE__; the sandbox gets it as a global.
const CODE = ts.transpileModule(SOURCE, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText

function load({ buildBase, windowBase } = {}) {
  const window = windowBase === undefined ? {} : { __AUTOCLIP_API_BASE__: windowBase }
  // TypeScript's CommonJS output writes exports.<name>, so the module object and
  // the locals must be the same object (this is what load-ts.cjs does too).
  const moduleExports = {}
  const context = {
    module: { exports: moduleExports },
    exports: moduleExports,
    window,
    console,
    setTimeout,
    clearTimeout,
    Promise,
  }
  if (buildBase !== undefined) context.__AUTOCLIP_API_BASE__ = buildBase
  runInSandbox(CODE, context)
  return moduleExports
}

test('same-origin build keeps the relative API path', () => {
  const { apiConfigManager } = load({ buildBase: '' })
  assert.equal(apiConfigManager.getBaseUrl(), '/api/v1')
  // A browser build without a configured backend still uses discovery.
  assert.equal(apiConfigManager.isReady(), false)
})

test('a build-time backend URL is used for the first request', () => {
  const { apiConfigManager } = load({ buildBase: 'https://engine.example.com' })
  assert.equal(apiConfigManager.getBaseUrl(), 'https://engine.example.com/api/v1')
  // Ready up front: nothing to wait for before the first call.
  assert.equal(apiConfigManager.isReady(), true)
  assert.equal(apiConfigManager.buildUrl('/health/'), 'https://engine.example.com/api/v1/health/')
})

test('a URL that already ends in /api/v1 is not doubled', () => {
  const { apiConfigManager } = load({ buildBase: 'https://engine.example.com/api/v1/' })
  assert.equal(apiConfigManager.getBaseUrl(), 'https://engine.example.com/api/v1')
  assert.equal(apiConfigManager.buildUrl('projects'), 'https://engine.example.com/api/v1/projects')
})

test('a runtime window override points one build at another backend', () => {
  const { apiConfigManager } = load({ buildBase: '', windowBase: 'http://127.0.0.1:8123' })
  assert.equal(apiConfigManager.getBaseUrl(), 'http://127.0.0.1:8123/api/v1')
  assert.equal(apiConfigManager.isReady(), true)
})

test('the build-time constant wins over the runtime override', () => {
  const { apiConfigManager } = load({ buildBase: 'https://build.example.com', windowBase: 'https://other.example.com' })
  assert.equal(apiConfigManager.getBaseUrl(), 'https://build.example.com/api/v1')
})

test('an empty or whitespace override is ignored, not turned into a bad URL', () => {
  const { apiConfigManager } = load({ buildBase: '   ', windowBase: '  ' })
  assert.equal(apiConfigManager.getBaseUrl(), '/api/v1')
  assert.equal(apiConfigManager.isReady(), false)
})
