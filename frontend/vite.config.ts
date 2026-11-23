import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import { sentryVitePlugin } from '@sentry/vite-plugin'
import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

// https://vitejs.dev/config/
export default defineConfig(({ mode, command }) => {
  const isProduction = mode === 'production'
  const root = fileURLToPath(new URL('.', import.meta.url))
  const env = loadEnv(mode, root, '')
  // Build-only token file; never expose SENTRY_* values via Vite's client env.
  const sentryEnv = loadEnv('sentry-build-plugin', root, 'SENTRY_')
  const authToken = sentryEnv.SENTRY_AUTH_TOKEN?.trim()
  const uploadRequested = sentryEnv.SENTRY_UPLOAD_SOURCEMAPS
  const uploadSourceMaps = command === 'build' &&
    (uploadRequested === 'true' || (uploadRequested !== 'false' && Boolean(authToken)))
  if (uploadSourceMaps && !authToken) {
    throw new Error('SENTRY_AUTH_TOKEN is required when SENTRY_UPLOAD_SOURCEMAPS=true')
  }
  // The desktop shell is not part of a web deployment, and .vercelignore keeps
  // it out of the build, so its version file may be absent. VITE_APP_VERSION
  // (or a fallback) must not make the web build depend on a desktop artifact.
  const readDesktopVersion = (): string => {
    try {
      return JSON.parse(readFileSync(new URL('../src-tauri/tauri.conf.json', import.meta.url), 'utf8')).version
    } catch {
      return '0.0.0'
    }
  }
  const desktopVersion = readDesktopVersion()
  const appVersion = env.VITE_APP_VERSION || desktopVersion
  // Dev-only: when the dev server is reached through a proxy that rewrites Host
  // (e.g. a container or cloud live-preview domain), Vite's host allowlist would
  // reject it. Opt in explicitly with VITE_DEV_ALLOWED_HOSTS; unset keeps Vite's
  // default localhost-only behaviour.
  const previewAllowedHosts = env.VITE_DEV_ALLOWED_HOSTS?.trim()
  
  return {
    plugins: [
      react(),
      ...(uploadSourceMaps ? [sentryVitePlugin({
        org: sentryEnv.SENTRY_ORG || 'autoclip-ts',
        project: sentryEnv.SENTRY_PROJECT || 'autoclip-frontend',
        authToken,
        telemetry: false,
        release: {
          // Must match src/desktop/sentry.ts exactly.
          name: `autoclip-frontend@${appVersion}`,
          inject: false,
        },
        sourcemaps: {
          assets: `${root}dist/**`,
          filesToDeleteAfterUpload: [`${root}dist/**/*.map`],
        },
      })] : []),
    ],
    define: {
      'import.meta.env.VITE_APP_VERSION': JSON.stringify(appVersion),
      // Hosted builds call a backend on another origin; apiConfig reads this
      // constant so the first request goes to the right host. Empty keeps the
      // relative /api/v1 path (dev server proxy, desktop, same-origin image).
      __AUTOCLIP_API_BASE__: JSON.stringify((env.VITE_API_BASE_URL || '').trim()),
      // Hosted function hosts reject a request body over 4.5 MB before it runs.
      // 0 disables the pre-upload check (self-hosted API, unlimited upload).
      __AUTOCLIP_UPLOAD_LIMIT_MB__: JSON.stringify(Number(env.VITE_UPLOAD_LIMIT_MB || 0) || 0),
    },
    base: isProduction ? './' : '/', // 生产环境使用相对路径
    optimizeDeps: {
      include: ['@tauri-apps/api', '@tauri-apps/api/dialog']
    },
    build: {
      sourcemap: uploadSourceMaps ? 'hidden' : false,
      assetsInlineLimit: 4096,
      chunkSizeWarningLimit: 1600,
      rollupOptions: {
        external: [],
        // NOTE: do NOT hand-split React and antd into separate vendor chunks.
        // antd's top-level code calls React.createContext at module-eval time;
        // when React and antd are in different chunks, the chunk load order is
        // not guaranteed and antd can evaluate before React's CJS-interop is
        // initialized, leaving `React` undefined → "Cannot read properties of
        // undefined (reading 'createContext')" → blank/black screen. Letting
        // Rollup decide chunking keeps React's evaluation ordered correctly.
      },
      // 生产环境禁用 Service Worker
      serviceWorker: false
    },
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
        '@provider-presets': fileURLToPath(new URL('../backend/presets/provider_presets.json', import.meta.url)),
        '@provider-quality': fileURLToPath(new URL('../backend/presets/provider_quality.json', import.meta.url)),
        '@event-catalog': fileURLToPath(new URL('../backend/core/event_catalog.json', import.meta.url)),
        '@failure-messages': fileURLToPath(new URL('../backend/core/failure_messages.json', import.meta.url)),
      },
    },
    server: {
      host: '0.0.0.0',
      port: 3000,
      strictPort: true, // 如果端口被占用则报错，而不是自动切换
      hmr: {
        overlay: false // 禁用错误覆盖层
      },
      // Comma-separated extra Host headers, e.g. the proxied preview domain.
      allowedHosts: previewAllowedHosts
        ? previewAllowedHosts.split(',').map((host) => host.trim()).filter(Boolean)
        : undefined,
      proxy: {
        '/api': {
          // Override when the desktop backend (random port) is running: BACKEND_URL=http://127.0.0.1:PORT npm run dev
          target: process.env.BACKEND_URL || 'http://localhost:8000',
          changeOrigin: true,
          // 预览域名是同源代理：后端看到的 Host 是 127.0.0.1:8000，而浏览器发的
          // Origin 是预览域名。原样转发它，后端的同源判定（Origin vs X-Forwarded-Host）
          // 才能放行写请求。
          configure: (proxy) => {
            proxy.on('proxyReq', (proxyReq, req) => {
              const origin = req.headers.origin
              if (origin) proxyReq.setHeader('X-Forwarded-Host', origin.replace(/^https?:\/\//, ''))
            })
          }
        }
      }
    }
  }
})
