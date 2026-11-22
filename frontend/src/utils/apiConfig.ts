/**
 * API 配置管理器
 * 处理动态后端地址和端口配置
 */

interface ApiConfig {
  baseUrl: string;
  port: number;
  isReady: boolean;
}

/**
 * Hosted builds point at a backend that is not on the same origin as the page.
 * The base URL arrives two ways, and both resolve before the first request, so
 * the bundle never waits for a Tauri backend-ready event that cannot fire in a
 * browser:
 *
 *  1. __AUTOCLIP_API_BASE__ — build-time constant injected by vite.config.ts
 *     from VITE_API_BASE_URL and inlined as a string literal.
 *  2. window.__AUTOCLIP_API_BASE__ — optional runtime override, so one build
 *     can be re-pointed without rebuilding.
 *
 * Same-origin builds leave both unset and keep the relative /api/v1 prefix.
 * `typeof` guards keep this safe in the node test sandbox, where neither exists.
 */
declare const __AUTOCLIP_API_BASE__: string;

function readEnvApiBaseUrl(): string {
  const candidates: unknown[] = [];
  try {
    if (typeof __AUTOCLIP_API_BASE__ !== 'undefined') candidates.push(__AUTOCLIP_API_BASE__);
  } catch {
    /* built without the define */
  }
  if (typeof window !== 'undefined') {
    candidates.push((window as any).__AUTOCLIP_API_BASE__);
  }
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim()) {
      return candidate.trim().replace(/\/+$/, '');
    }
  }
  return '';
}

const ENV_API_BASE_URL = readEnvApiBaseUrl();

const initialBaseUrl = ENV_API_BASE_URL
  ? (ENV_API_BASE_URL.endsWith('/api/v1') ? ENV_API_BASE_URL : `${ENV_API_BASE_URL}/api/v1`)
  : '/api/v1';

class ApiConfigManager {
  private static instance: ApiConfigManager;
  private config: ApiConfig = {
    baseUrl: initialBaseUrl,
    port: 0,
    // A configured remote backend needs no discovery step.
    isReady: Boolean(ENV_API_BASE_URL)
  };
  private listeners: Array<(config: ApiConfig) => void> = [];

  private constructor() {
    this.initializeConfig();
  }

  static getInstance(): ApiConfigManager {
    if (!ApiConfigManager.instance) {
      ApiConfigManager.instance = new ApiConfigManager();
    }
    return ApiConfigManager.instance;
  }

  private async initializeConfig() {
    // 检查是否在 Tauri 环境中
    if (typeof window !== 'undefined' && ((window as any).__TAURI__ || (window as any).__TAURI_INTERNALS__)) {
      try {
        // 监听后端启动事件
        const { listen } = await import('@tauri-apps/api/event');
        const { invoke } = await import('@tauri-apps/api/core');
        
        await listen('backend-started', (event: any) => {
          const backendStatus = event.payload;
          if (backendStatus && backendStatus.port) {
            this.updateFromPort(backendStatus.port);
          }
        });

        const backendStatus = await invoke('get_service_status') as any;
        if (backendStatus?.is_running && backendStatus?.port) {
          this.updateFromPort(backendStatus.port);
        }

        // 尝试从全局变量获取配置
        if ((window as any).__BACKEND_BASE__) {
          this.updateConfig({
            baseUrl: (window as any).__BACKEND_BASE__,
            port: this.extractPortFromUrl((window as any).__BACKEND_BASE__),
            isReady: true
          });
        }
      } catch (error) {
        console.warn('无法初始化 Tauri 事件监听:', error);
      }
    }
  }

  private updateFromPort(port: number) {
    this.updateConfig({
      baseUrl: `http://127.0.0.1:${port}/api/v1`,
      port,
      isReady: true
    });
  }

  private extractPortFromUrl(url: string): number {
    const match = url.match(/:(\d+)/);
    return match ? parseInt(match[1], 10) : 0;
  }

  private updateConfig(newConfig: Partial<ApiConfig>) {
    this.config = { ...this.config, ...newConfig };
    this.notifyListeners();
  }

  private notifyListeners() {
    // waitForReady 的监听器会在回调里把自己移除；直接 forEach 原数组会跳过后一个监听器，
    // 导致冷启动时排队的第 2、4… 个请求要等 30s 超时才发出（设置页打开慢就是这个原因）
    [...this.listeners].forEach(listener => listener(this.config));
  }

  /**
   * 获取当前 API 配置
   */
  getConfig(): ApiConfig {
    return { ...this.config };
  }

  /**
   * 获取 API 基础 URL
   */
  getBaseUrl(): string {
    return this.config.baseUrl;
  }

  /**
   * 检查 API 是否就绪
   */
  isReady(): boolean {
    return this.config.isReady;
  }

  /**
   * 添加配置变化监听器
   */
  addListener(listener: (config: ApiConfig) => void): () => void {
    this.listeners.push(listener);
    return () => {
      const index = this.listeners.indexOf(listener);
      if (index > -1) {
        this.listeners.splice(index, 1);
      }
    };
  }

  /**
   * 等待 API 就绪
   */
  async waitForReady(timeout: number = 30000, signal?: AbortSignal): Promise<boolean> {
    if (this.isReady()) {
      return true;
    }
    if (signal?.aborted) return false;

    return new Promise((resolve) => {
      const done = (ready: boolean) => {
        clearTimeout(timeoutId);
        removeListener();
        signal?.removeEventListener('abort', onAbort);
        resolve(ready);
      };
      const onAbort = () => done(false);
      const timeoutId = setTimeout(() => done(false), timeout);
      const removeListener = this.addListener((config) => {
        if (config.isReady) done(true);
      });
      signal?.addEventListener('abort', onAbort, { once: true });
    });
  }

  /**
   * 构建完整的 API URL
   */
  buildUrl(path: string): string {
    const normalizedPath = path.startsWith('/') ? path : `/${path}`;
    return `${this.config.baseUrl}${normalizedPath}`;
  }
}

// 导出单例实例
export const apiConfigManager = ApiConfigManager.getInstance();

// 导出便捷函数
export const isApiReady = () => apiConfigManager.isReady();
export const waitForApiReady = (timeout?: number) => apiConfigManager.waitForReady(timeout);
export const buildApiUrl = (path: string) => apiConfigManager.buildUrl(path);
