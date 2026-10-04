/**
 * 后端地址 / 访问密钥 / 请求封装。
 *
 * 设计要点（和原来那份单文件 index.html 保持一致）：
 *  · 同一份构建产物两种用法都支持 —— 本机同源打开什么都不用配；挂到 Vercel 这类静态托管上，
 *    用 `?api=https://后端地址&k=访问密钥` 打开一次就会记住（存 localStorage）。
 *  · 跨域**不能用 Cookie**（第三方 Cookie 会被浏览器拦），统一用 `X-Access-Key` 请求头。
 */

const LS_API = 'qjl_api';
const LS_KEY = 'qjl_key';

function query(name: string): string {
  try {
    return (new URLSearchParams(window.location.search).get(name) || '').trim();
  } catch {
    return '';
  }
}

function lsGet(name: string): string {
  try {
    return localStorage.getItem(name) || '';
  } catch {
    return '';
  }
}

function lsSet(name: string, value: string) {
  try {
    localStorage.setItem(name, value);
  } catch {
    // 隐私模式下 localStorage 可能不可写，忽略
  }
}

function normalizeBase(v: string): string {
  return (v || '').trim().replace(/\/+$/, '');
}

/** ?api= > localStorage > window.__QJL_API__ > 同源（空串） */
function resolveBase(): string {
  const fromUrl = normalizeBase(query('api'));
  if (fromUrl) {
    lsSet(LS_API, fromUrl);
    return fromUrl;
  }
  const cached = normalizeBase(lsGet(LS_API));
  if (cached) return cached;
  return normalizeBase((window as any).__QJL_API__ || '');
}

function resolveKey(): string {
  const fromUrl = query('k');
  if (fromUrl) {
    lsSet(LS_KEY, fromUrl);
    return fromUrl;
  }
  return lsGet(LS_KEY);
}

let _base = resolveBase();
let _key = resolveKey();

export function getApiBase(): string {
  return _base;
}

export function getAccessKey(): string {
  return _key;
}

export function setApiBase(v: string) {
  _base = normalizeBase(v);
  lsSet(LS_API, _base);
}

export function setAccessKey(v: string) {
  _key = (v || '').trim();
  lsSet(LS_KEY, _key);
}

/** 判断当前是不是「本机同源」模式 */
export function isSameOrigin(): boolean {
  return _base === '';
}

let unauthorizedHandler: (() => void) | null = null;

/** 收到 401 时的回调（App 里挂一个横幅提示） */
export function onUnauthorized(fn: (() => void) | null) {
  unauthorizedHandler = fn;
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers || {});
  if (_key) headers.set('X-Access-Key', _key);
  const res = await fetch(_base + path, { ...init, headers });
  if (res.status === 401 && unauthorizedHandler) unauthorizedHandler();
  return res;
}

async function errorText(res: Response): Promise<string> {
  try {
    const data = await res.json();
    return data?.detail || res.statusText;
  } catch {
    return res.statusText;
  }
}

export async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await apiFetch(path, init);
  if (!res.ok) throw new Error(await errorText(res));
  return (await res.json()) as T;
}

export function postJson<T>(path: string, body: unknown): Promise<T> {
  return apiJson<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/**
 * 读后端的 SSE 流（`/api/fetch` 用的就是它）。
 * 刻意用 fetch + ReadableStream 而不是 EventSource —— EventSource 不能带自定义请求头，
 * 跨域场景下就没法送 X-Access-Key 了。
 */
export async function streamEvents(
  path: string,
  body: unknown,
  onEvent: (ev: any) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await apiFetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) throw new Error(await errorText(res));
  const reader = res.body!.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const parts = buf.split('\n\n');
    buf = parts.pop() || '';
    for (const part of parts) {
      if (!part.startsWith('data: ')) continue;
      try {
        onEvent(JSON.parse(part.slice(6)));
      } catch {
        // 半截 JSON 直接跳过
      }
    }
  }
}
