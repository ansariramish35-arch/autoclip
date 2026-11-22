// The Vercel middleware is the only path from the browser to the engine. A bug
// here looks like "Request failed with status code 405/503" in the UI, which
// says nothing useful, so the contract is pinned by tests.
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const { runInSandbox } = require('./vm-sandbox.cjs')

const SOURCE = fs.readFileSync(path.join(__dirname, '../middleware.ts'), 'utf8')
const CODE = ts.transpileModule(SOURCE, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText

/** Minimal Next primitives: NextResponse is a Response with JSON helpers. */
function nextRuntime() {
  class NextResponse extends Response {
    static json(body, init) {
      return new NextResponse(JSON.stringify(body), {
        ...init,
        headers: { 'content-type': 'application/json', ...(init?.headers || {}) },
      })
    }
  }
  // Types only in the source: the transpiled file still requires the module.
  return { 'next/server': { NextResponse, NextRequest: class {} } }
}

/**
 * Load the middleware with a given AUTOCLIP_API_ORIGIN and a fetch double.
 * Returns { middleware, calls, config }.
 */
function load({ origin, respond, failWith } = {}) {
  const calls = []
  const moduleExports = {}
  const fetchImpl = async (url, init) => {
    calls.push({ url: String(url), init })
    if (failWith) throw failWith
    return respond ? respond(init) : new Response('{"ok":true}', {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  }
  // A URL-shaped object like Next's NextURL: only the fields the code reads.
  const makeRequest = (url, init) => {
    const parsed = new URL(url)
    const request = new Request(url, init)
    request.nextUrl = { pathname: parsed.pathname, search: parsed.search }
    return request
  }
  runInSandbox(CODE, {
    module: { exports: moduleExports },
    exports: moduleExports,
    console,
    process: { env: origin === undefined ? {} : { AUTOCLIP_API_ORIGIN: origin } },
    URL,
    URLSearchParams,
    Headers,
    Request,
    Response,
    AbortController,
    fetch: fetchImpl,
    require(id) {
      const mod = nextRuntime()[id]
      if (!mod) throw new Error(id)
      return mod
    },
  })
  return { middleware: moduleExports.default, config: moduleExports.config, calls, makeRequest }
}

test('a POST is forwarded to the engine with its method, path and body intact', async () => {
  const { middleware, calls, makeRequest } = load({ origin: 'https://engine.example.com' })
  const request = makeRequest('https://app.example.com/api/v1/studio/projects', {
    method: 'POST',
    headers: { origin: 'https://app.example.com', 'content-type': 'application/json' },
    body: JSON.stringify({ name: 'demo' }),
  })
  const response = await middleware(request)

  assert.equal(response.status, 200)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, 'https://engine.example.com/api/v1/studio/projects')
  assert.equal(calls[0].init.method, 'POST')
  assert.equal(new TextDecoder().decode(calls[0].init.body), '{"name":"demo"}')
})

test('the querystring survives the hop', async () => {
  const { middleware, calls, makeRequest } = load({ origin: 'https://engine.example.com' })
  await middleware(makeRequest('https://app.example.com/api/v1/projects?limit=10&state=done', { method: 'GET' }))
  assert.equal(calls[0].url, 'https://engine.example.com/api/v1/projects?limit=10&state=done')
})

test('GET sends no body, so the engine is not handed a phantom payload', async () => {
  const { middleware, calls, makeRequest } = load({ origin: 'https://engine.example.com' })
  await middleware(makeRequest('https://app.example.com/api/v1/health/', { method: 'GET' }))
  assert.equal(calls[0].init.body, undefined)
})

test('x-forwarded-host carries the browser Host so the origin guard sees a match', async () => {
  const { middleware, calls, makeRequest } = load({ origin: 'https://engine.example.com' })
  await middleware(makeRequest('https://app.example.com/api/v1/x', {
    method: 'POST',
    headers: { host: 'app.example.com', origin: 'https://app.example.com' },
  }))
  const sent = calls[0].init.headers
  assert.equal(sent.get('x-forwarded-host'), 'app.example.com')
  // The origin is forwarded untouched: the engine must judge the real caller.
  assert.equal(sent.get('origin'), 'https://app.example.com')
})

test('hop-by-hop headers are not forwarded', async () => {
  const { middleware, calls, makeRequest } = load({ origin: 'https://engine.example.com' })
  await middleware(makeRequest('https://app.example.com/api/v1/x', {
    method: 'POST',
    headers: { 'transfer-encoding': 'chunked', 'accept-encoding': 'gzip', connection: 'keep-alive' },
  }))
  const sent = calls[0].init.headers
  for (const header of ['transfer-encoding', 'accept-encoding', 'connection', 'host']) {
    assert.equal(sent.get(header), null, `${header} must be dropped`)
  }
})

test('an unset engine origin answers 503 with a readable code, not a bare 405', async () => {
  for (const origin of [undefined, '', '   ']) {
    const { middleware, calls, makeRequest } = load({ origin })
    const response = await middleware(makeRequest('https://app.example.com/api/v1/health/', { method: 'POST' }))
    assert.equal(response.status, 503)
    const body = await response.json()
    assert.equal(body.error_code, 'engine_not_configured')
    assert.ok(body.detail.length > 0)
    assert.equal(calls.length, 0, 'nothing may be fetched without a target')
  }
})

test('a trailing slash on the configured origin does not double up', async () => {
  const { middleware, calls, makeRequest } = load({ origin: 'https://engine.example.com/' })
  await middleware(makeRequest('https://app.example.com/api/v1/health/', { method: 'GET' }))
  assert.equal(calls[0].url, 'https://engine.example.com/api/v1/health/')
})

test('a dead engine answers 502 instead of throwing through the proxy', async () => {
  const { middleware, makeRequest } = load({
    origin: 'https://engine.example.com',
    failWith: Object.assign(new Error('connect ECONNREFUSED'), { name: 'TypeError' }),
  })
  const response = await middleware(makeRequest('https://app.example.com/api/v1/health/', { method: 'GET' }))
  assert.equal(response.status, 502)
  const body = await response.json()
  assert.equal(body.error_code, 'engine_unreachable')
  // The raw message can contain internal addresses; only the type is exposed.
  assert.equal(body.reason, 'TypeError')
})

test('the engine status, body and content-type pass through', async () => {
  const { middleware, makeRequest } = load({
    origin: 'https://engine.example.com',
    respond: () => new Response('{"detail":"bad input"}', {
      status: 422, headers: { 'content-type': 'application/json' },
    }),
  })
  const response = await middleware(makeRequest('https://app.example.com/api/v1/x', { method: 'POST', body: '{}' }))
  assert.equal(response.status, 422)
  assert.equal(response.headers.get('content-type'), 'application/json')
  assert.deepEqual(await response.json(), { detail: 'bad input' })
})

test('multiple set-cookie headers stay separate', async () => {
  const respond = () => {
    const headers = new Headers()
    headers.append('set-cookie', 'a=1; Path=/')
    headers.append('set-cookie', 'b=2; Path=/')
    return new Response('ok', { status: 200, headers })
  }
  const { middleware, makeRequest } = load({ origin: 'https://engine.example.com', respond })
  const response = await middleware(makeRequest('https://app.example.com/api/v1/x', { method: 'GET' }))
  assert.deepEqual(response.headers.getSetCookie(), ['a=1; Path=/', 'b=2; Path=/'])
})

test('content-encoding is dropped: the proxy body is decoded already', async () => {
  const respond = () => new Response('{}', {
    status: 200,
    headers: { 'content-encoding': 'gzip', 'content-type': 'application/json' },
  })
  const { middleware, makeRequest } = load({ origin: 'https://engine.example.com', respond })
  const response = await middleware(makeRequest('https://app.example.com/api/v1/x', { method: 'GET' }))
  assert.equal(response.headers.get('content-encoding'), null)
})

test('the matcher covers every /api path and nothing else', () => {
  const { config } = load({ origin: 'https://engine.example.com' })
  // Spread first: the sandbox's Array is a different realm, so deepEqual would
  // compare prototypes rather than values.
  assert.deepEqual([...config.matcher], ['/api/:path*'])
  assert.equal(config.runtime, 'nodejs')
})

// The engine answers some paths with a trailing-slash redirect. Its Location
// header names the engine, which the browser cannot reach and must not learn, so
// the proxy rewrites that origin back to the public one.
test('a redirect Location pointing at the engine is rewritten to the public origin', async () => {
  const respond = () => new Response(null, {
    status: 307,
    headers: { location: 'https://engine.example.com/api/v1/settings/' },
  })
  const { middleware, makeRequest } = load({ origin: 'https://engine.example.com', respond })
  const response = await middleware(makeRequest('https://app.example.com/api/v1/settings', { method: 'GET' }))
  assert.equal(response.status, 307)
  assert.equal(response.headers.get('location'), 'https://app.example.com/api/v1/settings/')
})

test('a Location pointing somewhere else is left alone', async () => {
  const respond = () => new Response(null, {
    status: 302,
    headers: { location: 'https://oauth.example.org/authorize?x=1' },
  })
  const { middleware, makeRequest } = load({ origin: 'https://engine.example.com', respond })
  const response = await middleware(makeRequest('https://app.example.com/api/v1/login', { method: 'GET' }))
  assert.equal(response.headers.get('location'), 'https://oauth.example.org/authorize?x=1')
})

test('an engine redirect with an origin-only Location becomes the app root', async () => {
  const respond = () => new Response(null, {
    status: 307,
    headers: { location: 'https://engine.example.com' },
  })
  const { middleware, makeRequest } = load({ origin: 'https://engine.example.com', respond })
  const response = await middleware(makeRequest('https://app.example.com/api/v1/ping', { method: 'GET' }))
  assert.equal(response.headers.get('location'), 'https://app.example.com/')
})
