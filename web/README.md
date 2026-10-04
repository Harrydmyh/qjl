# 开团助手 · 前端（组件化）

把原来那份 93KB 的单文件 `index.html` 拆成了 React 组件。**后端不在这个目录里** ——
后端（`server.py` / `core.py`）常驻在你自己的机器上，这个页面只负责界面和发请求。

## 跑起来

```bash
npm install
npm run dev      # 开发： http://localhost:5173
npm run build    # 产出静态文件到 dist/（先跑 tsc 类型检查）
npm run smoke    # 冒烟测试：真后端 + 真 DOM，验证页面能跑、能连通
```

### `npm run smoke` 会验三件事

用 esbuild 把源码打成 IIFE，在 jsdom 里真跑一遍，配真后端：

| 场景 | 验的是 |
| --- | --- |
| A 同源 | 本机打开页面的用法（相对路径请求） |
| B 跨域 + `?api=&k=` | **Vercel 的真实用法**：跨域请求打到后端，并模拟隧道流量（带 `x-forwarded-for`），必须有密钥才放行 |
| C 跨域不带密钥 | 应该被 401 挡住并弹出「需要访问密钥」提示 |

它**不会**动你已经在跑的后端：8765 上已有服务就直接复用、不会杀；只有没服务时才自己起一个，并在结束时回收。

## 怎么连上后端

两种用法，同一份构建产物：

| 场景 | 怎么打开 |
| --- | --- |
| 后端和页面在同一台机器 | 直接开 `http://127.0.0.1:8765/`，同源，什么都不用配 |
| 页面挂在 Vercel 这类静态托管上 | `<页面地址>/?api=https://后端地址&k=访问密钥` 打开**一次**，之后自动记住 |
| 开发时（vite 5173） | 在「设置」页填后端地址和密钥，或同样用 `?api=&k=` |

细节：

- 后端地址/密钥存在浏览器 `localStorage`（`qjl_api` / `qjl_key`），优先级
  `?api=` → localStorage → `window.__QJL_API__` → 同源。
- 跨域请求**用 `X-Access-Key` 请求头带密钥，不用 Cookie** —— 第三方 Cookie 会被浏览器直接拦掉。
- 后端必须是 **HTTPS**：页面是 https 的话，http 后端会被当混合内容拦掉。
- 后端那边已经开了 CORS，并且「本机免密钥、外部来源必须带密钥」。

## 目录结构

```
src/
  main.tsx              入口
  App.tsx               壳：侧边栏 + 视图切换 + 弹窗 + 连接状态
  types.ts              和后端 core.parse_item 对应的类型
  lib/
    api.ts              后端地址/密钥解析、apiFetch、SSE 流读取
    rules.ts            业务判据（能不能开、是否冷却中、默认勾选…）
    ui.ts               Ui / View / LogLine 等共享类型
  hooks/
    useJob.ts           开团任务状态（服务端任务，关页面也继续，重开能接回）
  components/
    Sidebar.tsx         侧边栏
    Dialog.tsx          页内弹窗（不用原生 alert/confirm）
    ActivityTable.tsx   活动表格（列表和开团面板共用）
    FetchView.tsx       刷新列表（流式进度）
    OpenView.tsx        活动 & 开团（筛选、勾选、间隔、定时、进度、日志）
    LogView.tsx         开团记录
    SettingsView.tsx    后端连接 + 服务端凭证
```

## 部署到 Vercel

在 Vercel 里导入本仓库，把 **Root Directory 设成 `web`**（Framework 会自动识别成 Vite），
Build Command `npm run build`，Output Directory `dist`。

部署完用 `https://你的域名/?api=https://后端地址&k=访问密钥` 打开一次即可。

## 注意

- 后端任务跑在**服务端**，所以关掉页面不会中断开团；但后端进程如果重启，内存里的任务会丢。
- 页面里**不含任何密钥**，密钥是运行时通过 `?k=` 传进来、存在浏览器本地的。
- 改判据（能不能开团、冷却规则）是改 `src/lib/rules.ts`；改接口调用是改 `src/lib/api.ts`。
