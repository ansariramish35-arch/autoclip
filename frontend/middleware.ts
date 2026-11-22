/**
 * Same-origin proxy from the Vercel frontend to the AutoClip engine.
 *
 * Why a proxy at all: the browser must talk to ONE origin. The backend's origin
 * guard (backend/core/local_origin_guard.py) only accepts a write whose Origin
 * matches the request Host, so a cross-origin call needs an allowlist and a CORS
 * preflight. Rewriting here means the engine sees the browser's own Host and
 * every write just works.
 *
 * Why middleware and not vercel.json rewrites: a rewrite destination is static
 * JSON, so changing the engine host would mean editing this file and pushing.
 * A middleware reads AUTOCLIP_API_ORIGIN at runtime instead — set or change it
 * in Vercel → Settings → Environment Variables, then redeploy.
 *
 * Unset behaviour is deliberate: /api/* answers 503 with a readable message.
 * Without this, a POST /api/... falls through to the SPA catch-all and static
 * hosting rejects it with a bare 405 ("Request failed with status code 405"),
 * which says nothing about what is actually wrong.
 */
import { NextResponse, type NextRequest } from 'next/server'

/** Node runtime: no edge-runtime API is needed, and buffered requests avoid streaming limits. */
export const config = {
  matcher: ['/api/:path*'],
  runtime: 'nodejs',
}

const TARGET = (process.env.AUTOCLIP_API_ORIGIN || '').trim().replace(/\/+$/, '')

/** Headers that describe the hop, not the request: forwarding them corrupts framing. */
const HOP_BY_HOP = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
  'host',
  'content-length',
  // Let the proxy target compress; the browser and Vercel handle the outer hop.
  'accept-encoding',
])

function notConfigured(): NextResponse {
  return NextResponse.json(
    {
      detail: '尚未配置 AutoClip 引擎地址（AUTOCLIP_API_ORIGIN）。',
      error_code: 'engine_not_configured',
    },
    { status: 503 },
  )
}

export default async function middleware(request: NextRequest): Promise<Response> {
  if (!TARGET) return notConfigured()

  const target = new URL(`${TARGET}${request.nextUrl.pathname}${request.nextUrl.search}`)

  const headers = new Headers()
  request.headers.forEach((value, key) => {
    if (!HOP_BY_HOP.has(key.toLowerCase())) headers.set(key, value)
  })
  // The guard compares Origin against the Host the engine sees on this hop.
  headers.set('x-forwarded-host', request.headers.get('host') || target.host)

  const method = request.method.toUpperCase()
  const body = method === 'GET' || method === 'HEAD' ? undefined : await request.arrayBuffer()

  let upstream: Response
  try {
    upstream = await fetch(target, {
      method,
      headers,
      body,
      // Let callers abort (navigating away from a long import must not hang).
      signal: request.signal,
      redirect: 'manual',
      cache: 'no-store',
    })
  } catch (error) {
    return NextResponse.json(
      {
        detail: '无法连接 AutoClip 引擎，请确认容器服务正在运行。',
        error_code: 'engine_unreachable',
        // Type only: the message can echo internal addresses.
        reason: (error as Error)?.name || 'Error',
      },
      { status: 502 },
    )
  }

  const responseHeaders = new Headers()
  upstream.headers.forEach((value, key) => {
    const lower = key.toLowerCase()
    if (lower === 'content-encoding' || lower === 'content-length' || lower === 'transfer-encoding') return
    if (lower === 'set-cookie') return // handled below, one per cookie
    responseHeaders.set(key, value)
  })
  // getSetCookie keeps multiple cookies distinct; Headers.forEach would join them.
  for (const cookie of upstream.headers.getSetCookie?.() ?? []) {
    responseHeaders.append('set-cookie', cookie)
  }

  return new NextResponse(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  })
}
