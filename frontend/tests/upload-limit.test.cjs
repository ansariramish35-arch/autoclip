// The hosted deployment cannot receive a body over 4.5 MB, so the UI must say so
// instead of letting the browser report a network error.
const test = require('node:test')
const assert = require('node:assert')
const fs = require('node:fs')
const path = require('node:path')

const source = fs.readFileSync(path.join(__dirname, '..', 'src', 'features', 'studio', 'uploadLimit.ts'), 'utf8')

test('the limit is opt-in, so a self-hosted API keeps unlimited uploads', () => {
  // A build-time define, not import.meta: the tests load this module as CommonJS.
  assert.match(source, /__AUTOCLIP_UPLOAD_LIMIT_MB__/)
  assert.doesNotMatch(source, /import\.meta/)
  assert.match(source, /raw > 0 \? Math\.floor\(raw \* 1024 \* 1024\) : 0/)
  assert.match(source, /typeof __AUTOCLIP_UPLOAD_LIMIT_MB__ === 'number'/)
  const config = fs.readFileSync(path.join(__dirname, '..', 'vite.config.ts'), 'utf8')
  assert.match(config, /__AUTOCLIP_UPLOAD_LIMIT_MB__: JSON\.stringify\(Number\(env\.VITE_UPLOAD_LIMIT_MB \|\| 0\) \|\| 0\)/)
})

test('the message names the real limit and the way around it', () => {
  assert.match(source, /4\.5 MB/)
  assert.match(source, /链接/)
})

test('a missing size is not treated as too large', () => {
  assert.match(source, /typeof bytes !== 'number' \|\| bytes <= 0/)
})

test('the import path checks the video before posting it', () => {
  const api = fs.readFileSync(path.join(__dirname, '..', 'src', 'features', 'studio', 'api.ts'), 'utf8')
  assert.match(api, /uploadLimitError\(\(body\.get\('video'\)/)
  assert.match(api, /return Promise\.reject\(new Error\(tooLarge\)\)/)
})

test('the hosted build sets the limit and a local build does not', () => {
  const script = fs.readFileSync(path.join(__dirname, '..', '..', 'scripts', 'vercel-build.sh'), 'utf8')
  assert.match(script, /VITE_UPLOAD_LIMIT_MB="\$\{AUTOCLIP_UPLOAD_LIMIT_MB:-4\.2\}"/)
})
