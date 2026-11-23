/**
 * The request-body cap of a hosted deployment, explained before the upload fails.
 *
 * Vercel rejects a request over 4.5 MB at the edge, before the function runs, so a
 * larger video does not fail with our error envelope — the browser sees a network
 * error or a bare 413. That is indistinguishable from a broken site, which is
 * exactly the wrong impression. Checking the size here turns it into one sentence
 * that says what happened and what to do instead.
 *
 * A self-hosted API has no such cap, so the limit is opt-in: the build replaces
 * `__AUTOCLIP_UPLOAD_LIMIT_MB__` for the hosted build only (see vite.config.ts).
 * A plain build-time global rather than a Vite env accessor, because the tests
 * load this module into a CommonJS sandbox where that syntax is not available.
 */
declare const __AUTOCLIP_UPLOAD_LIMIT_MB__: number | undefined
const raw = typeof __AUTOCLIP_UPLOAD_LIMIT_MB__ === 'number' ? __AUTOCLIP_UPLOAD_LIMIT_MB__ : 0

/** Bytes a single upload may occupy, or 0 when the deployment has no cap. */
export const UPLOAD_LIMIT_BYTES = Number.isFinite(raw) && raw > 0 ? Math.floor(raw * 1024 * 1024) : 0

export const UPLOAD_LIMIT_MESSAGE =
  '这个部署每次上传最多 4.5 MB（Vercel 的函数请求体上限）。请改用视频链接导入，或先压缩视频再上传；字幕文件不受影响。'

/** True when this file cannot be posted to the API of this deployment. */
export function exceedsUploadLimit(bytes: number | undefined | null): boolean {
  if (!UPLOAD_LIMIT_BYTES || typeof bytes !== 'number' || bytes <= 0) return false
  return bytes > UPLOAD_LIMIT_BYTES
}

/** The message to show for a file, or null when it can be uploaded. */
export function uploadLimitError(bytes: number | undefined | null): string | null {
  return exceedsUploadLimit(bytes) ? UPLOAD_LIMIT_MESSAGE : null
}
