// 冒烟测试：真后端 + 真源码（esbuild 打成 IIFE）+ jsdom 真 DOM。
// 验证的是「能跑起来、能渲染、能连通后端」，不是「能编译」。
//
// 三个场景：
//   A 同源        —— 本机打开页面的用法
//   B 跨域+密钥   —— Vercel 页面 ?api=&k= 的用法（并且模拟隧道流量，必须有密钥才放行）
//   C 跨域无密钥  —— 应该被 401 挡住并给出提示
import { spawn, spawnSync } from 'node:child_process';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { join, extname } from 'node:path';

const ROOT = '/Users/apple/noah-chat/qjl';
const WEB = ROOT + '/web';
const PY = '/opt/homebrew/bin/python3';
const ESBUILD = WEB + '/node_modules/.bin/esbuild';
const API = 'http://127.0.0.1:8765';
const PORT = 8766;
const BUNDLE = '/tmp/qjl_smoke_bundle.js';

const cleanEnv = { ...process.env };
for (const k of ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']) delete cleanEnv[k];
cleanEnv.NO_PROXY = 'localhost,127.0.0.1,::1';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const FAILS = [];
const check = (label, got, want) => {
  const ok = String(got) === String(want);
  if (!ok) FAILS.push(label);
  console.log(`    ${ok ? 'PASS' : 'FAIL'}  ${label} → ${got}（期望 ${want}）`);
};

async function waitPort(url, tries = 40) {
  for (let i = 0; i < tries; i++) {
    try { await fetch(url, { signal: AbortSignal.timeout(1200) }); return true; } catch { await sleep(400); }
  }
  return false;
}

// ── 1) 打包 ──
console.log('[1/4] esbuild 打包源码 …');
const build = spawnSync(ESBUILD,
  ['src/main.tsx', '--bundle', '--format=iife', '--loader:.css=text',
   '--define:process.env.NODE_ENV="production"', `--outfile=${BUNDLE}`],
  { cwd: WEB, encoding: 'utf8' });
if (build.status !== 0) { console.error('esbuild 失败：\n' + (build.stderr || build.stdout)); process.exit(1); }
const bundle = await readFile(BUNDLE, 'utf8');
console.log('      ' + Math.round(bundle.length / 1024) + ' KB');

// ── 2) 后端：已有就复用，没有才自己起（起了必须回收，否则会在 8765 上留垃圾进程）──
console.log('[2/4] 准备后端 …');
const preexisting = await waitPort(API + '/api/access', 2);
let backend = null;
if (preexisting) {
  console.log('      8765 上已经有后端在跑，直接复用（不会动它）');
} else {
  console.log('      没有现成的，自己起一个（跑完会收掉）');
  backend = spawn(PY, ['server.py'], { cwd: ROOT, env: cleanEnv, stdio: 'ignore', detached: true });
  if (!(await waitPort(API + '/api/access'))) { console.error('后端起不来'); process.exit(1); }
}
const KEY = JSON.parse(await readFile(ROOT + '/data/config.json', 'utf8')).access_key || '';
console.log('      access_key ' + (KEY ? '已启用' : '未启用（跨域测试会跳过鉴权断言）'));

// ── 3) 静态服务；/api 反代只给「同源」场景用 ──
console.log('[3/4] 起静态服务 …');
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };
const server = createServer(async (req, res) => {
  if (req.url.startsWith('/api')) {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const r = await fetch(API + req.url, {
      method: req.method,
      headers: { 'content-type': req.headers['content-type'] || 'application/json' },
      body: chunks.length ? Buffer.concat(chunks) : undefined,
    });
    res.writeHead(r.status, { 'content-type': r.headers.get('content-type') || 'application/json' });
    res.end(Buffer.from(await r.arrayBuffer()));
    return;
  }
  try {
    const file = join(WEB, 'dist', req.url === '/' ? 'index.html' : req.url.split('?')[0].slice(1));
    const buf = await readFile(file);
    res.writeHead(200, { 'content-type': MIME[extname(file)] || 'application/octet-stream' });
    res.end(buf);
  } catch { res.writeHead(404).end('not found'); }
});
await new Promise((r) => server.listen(PORT, r));

// ── 4) 各场景 ──
const { JSDOM } = await import(WEB + '/node_modules/jsdom/lib/api.js');

async function scenario(name, { search = '', viaApi = false, fakeProxy = false }) {
  console.log(`\n  【${name}】`);
  const errors = [];
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: `http://127.0.0.1:${PORT}/${search}`,
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    beforeParse(window) {
      window.fetch = (input, init) => {
        const url = new URL(typeof input === 'string' ? input : input.url, `http://127.0.0.1:${PORT}/`);
        // 只看后端请求；静态资源仍然走本地静态服务
        if (url.port !== '8766' && fakeProxy) {
          // ⚠️ 不能写成 {...init.headers} —— Headers 对象用展开运算符会变成空对象，
          // 把 apiFetch 加的 X-Access-Key 头丢掉（踩过）。
          const h = new Headers((init && init.headers) || {});
          h.set('x-forwarded-for', '1.2.3.4');
          init = { ...(init || {}), headers: h };
        }
        if (viaApi && url.port === '8766' && url.pathname.startsWith('/api')) {
          // 跨域场景不走反代，直接打到真后端
          url.protocol = 'http:';
          url.host = '127.0.0.1:8765';
        }
        return fetch(url, init);
      };
      window.Headers = Headers;
      window.Request = Request;
      window.Response = Response;
      window.AbortController = AbortController;
      window.addEventListener('error', (e) => errors.push('error: ' + (e.error?.stack || e.message)));
      window.addEventListener('unhandledrejection', (e) => errors.push('rejection: ' + e.reason));
    },
  });
  dom.window.document.body.appendChild(
    Object.assign(dom.window.document.createElement('script'), { textContent: bundle }));
  await sleep(3200);
  const doc = dom.window.document;
  const txt = (sel) => (doc.querySelector(sel)?.textContent || '').replace(/\s+/g, ' ').trim();
  const info = txt('.topbar .muted');
  const banner = txt('.banner.err');
  const rows = doc.querySelectorAll('tbody tr').length;
  const m = info.match(/(\d+)\s*条/);
  return { info, banner, rows, count: m ? Number(m[1]) : 0, errors, hasSidebar: !!doc.querySelector('.sidebar') };
}

console.log('[4/4] jsdom 场景测试');

const a = await scenario('A 同源（本机打开）', {});
check('侧边栏渲染', a.hasSidebar, true);
check('读到活动条数 > 0', a.count > 0, true);
check('无运行时错误', a.errors.length, 0);
console.log('       顶栏：' + a.info);

const b = await scenario('B 跨域 + ?api=&k=（Vercel 用法，模拟隧道流量）', {
  search: `?api=${encodeURIComponent(API)}${KEY ? '&k=' + KEY : ''}`,
  viaApi: true,
  fakeProxy: !!KEY,
});
check('侧边栏渲染', b.hasSidebar, true);
check('跨域也读到了活动条数', b.count > 0, true);
check('没有 401 横幅', b.banner.includes('需要访问密钥'), false);
check('无运行时错误', b.errors.length, 0);
console.log('       顶栏：' + b.info);
if (b.errors.length) b.errors.slice(0, 3).forEach((e) => console.log('       · ' + String(e).slice(0, 220)));

if (KEY) {
  const c = await scenario('C 跨域但不带密钥（应该被挡）', {
    search: `?api=${encodeURIComponent(API)}`,
    viaApi: true,
    fakeProxy: true,
  });
  check('显示了「需要访问密钥」提示', c.banner.includes('需要访问密钥'), true);
  check('没读到活动（被挡住）', c.count, 0);
}

server.close();
if (backend) {
  try {
    process.kill(-backend.pid);
  } catch {}
  // 等它真的退出（uvicorn 收到 SIGTERM 要几百毫秒才关掉监听）
  for (let i = 0; i < 20; i++) {
    await sleep(300);
    let alive = true;
    try {
      await fetch(API + '/api/access', { signal: AbortSignal.timeout(800) });
    } catch {
      alive = false;
    }
    if (!alive) break;
  }
  console.log('已回收自己起的后端');
}
console.log('\n' + (FAILS.length ? `✗ 失败 ${FAILS.length} 项: ${FAILS.join(' / ')}` : '✔ 三个场景全部通过'));
process.exit(FAILS.length ? 1 : 0);
