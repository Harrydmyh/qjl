# 项目长期注意事项（开团助手 / 群接龙）

## 运行方式
- **现状（2026-10-02 09:53 清理后）：只保留 `python3 server.py` 这一条启动路径。**
  启动：`cd /Users/apple/noah-chat/qjl && python3 server.py`；停：在该终端按 `Ctrl+C`。
  - **已删**（都移到废纸篓 `~/.Trash/qjl-清理-2026-10-02/`，可恢复）：
    `start.command`、`start-daemon.command`、`stop.command`、`stop.sh`、
    `发布前端.command`、`开启远程访问.command`、
    以及 `~/Library/LaunchAgents/com.qjl.opener.plist`。
  - **launchd 常驻已卸**（`launchctl bootout gui/501/com.qjl.opener` → exit=0，
    `launchctl print` 已查不到；8765 已释放）→ **重启机器不会再自动起**。
  - **用户后续计划：把服务搬到阿里云** → 别再往「本机常驻 / 内网穿透 / 双击脚本」方向加东西。
- 服务监听 `http://127.0.0.1:8765`；页面每次请求都从磁盘读 `index.html`（带 `no-store`），
  **改前端不用重启**；改 `core.py` / `server.py` **必须重启**才生效。
- 用 **系统 Python**：`/opt/homebrew/bin/python3`。项目自带的 managed python 3.13.12 **没装 fastapi**。
- 别用 `nohup ... &` / 工具的 run_in_background 起服务（进程活不过那次调用，已踩过多次）。
  要起后台的只能用 `start_new_session=True`（setsid 等价物）。
- （历史）原先用 `lsof` 精确杀端口而不是 `pkill -f server.py`（会误杀工具自身）——脚本虽删，
  这条经验仍适用。**`stop.sh` 已删，要停服务用**：
  - 自己终端里起的 → `Ctrl+C`；
  - agent 用 `start_new_session=True` 起的（挂着、没有终端）→
    `lsof -nP -iTCP:8765 -sTCP:LISTEN -t | xargs kill`
- ⚠️ **清理后必然后果**：用户点「刷新列表」会报 `TypeError: Failed to fetch`（页面还开着、
  后端已停）。**先 `lsof` 确认有没有监听，没有就是没起服务** —— 别去查代码/跨域配置。

## 电源 / 合盖运行（2026-10-02 查证）
硬件：**MacBook Pro M3（Mac15,3）、macOS 15.7.2**，**只有内建屏**（无外接显示器）。
- `pmset -g custom` 列出的键：ACPower/BatteryPower、sleep、displaysleep、disksleep、standby、
  hibernatemode、powernap、womp、networkoversleep、tcpkeepalive、ttyskeepawake、lowpowermode、
  lessbright、hibernatefile、SleepOnPowerButton —— **不含 `disablesleep`**，
  **但 `disablesleep` 实际是有效的键**。
- **怎么无 sudo 判断 pmset 认不认某个键（可复用技巧）**：跑 `pmset -a <key> 1` 看报错——
  键有效 → `'pmset' must be run as root...`；键无效 → `Usage: pmset <options>`。
  实测 `disablesleep` 报前者、`nosuchkey` 报后者。（`man pmset` 里**没有** disablesleep 条目，别靠 man 判断。）
- 现状设置：**AC `sleep 0`（插电永不睡）**、`displaysleep 0`（插电屏也不黑）；**Battery `sleep 20` 分钟**。
- **合盖必睡**（无外接屏时，插电也一样）→ 想合盖继续跑要 `sudo pmset -a disablesleep 1`
  （恢复 `... 0`），且**必须插电**（电池 20 分钟会睡；长期插电靠「优化电池充电」护电池）。
- ⚠️ 合盖运行**散热变差**，要放硬面、别垫床/沙发；本项目负载很轻，一般没问题。
- 备选硬件方案：**HDMI/DP 欺骗器（headless dummy plug，约 ¥20）** → macOS 以为接了外屏，
  原生 clamshell 模式保持唤醒，不用改系统设置。
- 三条路取舍：**机器平时放家里不动 → 改设置最省事（¥0）；机器要带走 → 只能上云**。

## 常驻 / 远程访问（2026-09-30 加，**2026-10-02 状态：本机常驻已卸、Tailscale 待卸载**）

> ⚠️ **方案已改**：用户 10-02 说「后面我看看把服务搬上阿里云」，所以「本机当服务器 + 内网穿透」
> 这条路**先作废**，双击脚本也都删了。但下面这些结论**仍然有效、搬云要复用**：
> **访问密钥闸门**和 **CORS/`?api=` 跨域前端**这两块是公网部署的必需品，**别删代码**。
- **网络事实**：内网 `10.88.49.26`、出口 `18.136.28.35`（新加坡 AWS）→ 在 NAT 后面，
  **没有可直连的公网 IP**（所以当初才只能走隧道）。
- **访问密钥闸门**：`config.json` 里的 `access_key`，设了才生效（默认不设 = 不启用）。
  - **本机直连免密钥**（对端 127.0.0.1/::1 且无 `x-forwarded-for`/`cf-connecting-ip` 等代理头）；
    其它来源（局域网 IP、隧道/反代流量）必须带密钥：cookie / `?k=` / `X-Access-Key`。
    带 `?k=` 一次自动种 cookie（HttpOnly/180 天），前端无需改动。
  - 接口：`GET /api/access`、`POST /api/access/rotate|disable`。`GET /api/config` 会过滤掉 access_key。
  - 不用 HTTP Basic（嵌入式预览里原生弹窗会被吞）。前端遇 401 会在页顶插红条提示。
  - ✅ 现状：`access_key` **仍启用着**（`{enabled:true,length:24}`），本机用不受影响；
    **搬阿里云正好用它当门槛**，`strict_access_key` 上云后建议设 True（有反代时必开）。
- **launchd 常驻：已卸除**（2026-10-02）。历史记录：`~/Library/LaunchAgents/com.qjl.opener.plist`
  （RunAtLoad+KeepAlive+ThrottleInterval=10）曾被用户双击脚本成功启用，已验证过开机自启 + 崩溃拉起。
  ⚠️ **agent 自己跑 `launchctl bootstrap` 会报 `Bootstrap failed: 5: Input/output error`**
  （gui/user 域都一样，关沙箱也不行；plist 本身是合法的）——注册必须在用户交互会话里做。
  **但 `launchctl print` / `kickstart` / `bootout` 在 agent 里都能用**（只有 `bootstrap` 挂）。
  - **体检三连**（缺一不可，别靠 ps/日志猜来路）：① `lsof -nP -iTCP:8765 -sTCP:LISTEN`（在不在跑）
    ② `launchctl print gui/$(id -u)/com.qjl.opener | grep -E "state|pid|properties|runs"`（有没有被托管）
    ③ `tailscale status` + `ls /var/run/tailscaled.socket`（隧道起没起）。
    **「服务在跑」≠「已常驻」** —— 这两件事被误判过（pid 57612 其实是 launchd 的子进程，
    因为它写 `/tmp/qjl_server.log` 就以为是我手起的，其实 plist 的 StandardOutPath 就指那儿）。
  - **验证崩溃自愈 / 重启服务的正确姿势**：`launchctl kickstart -k gui/$(id -u)/com.qjl.opener`
    → 会重启并让 `runs` +1、pid 变新。比 `kill` 安全（keepalive 兜底）。
- **Tailscale：装了但没用上，待卸载**（`brew install tailscale` 1.102.5）。
  daemon 起来了（`/var/run/tailscaled.socket` 在），但一直卡在 `BackendState = NeedsLogin`
  **从未登录成功、也没有任何 serve 配置 / tailnet 设备** → **Tailscale 那边不需要清理**。
  ⚠️ **agent 里 `sudo` 被禁**（`Operation not permitted`），而 brew 把 tailscale 服务以 root 起 →
  **停止/卸载只能用户自己做**：
  ```
  sudo brew services stop tailscale
  sudo brew uninstall tailscale      # brew 提示过：部分路径已被 root 取得所有权，
                                     # 卸载时可能要按提示 sudo rm 手动清
  ```
  （`tailscale down` 非 root 能跑、无报错，但停不掉 daemon。）
  - **为什么当初起不来**：macOS 上 Tailscale **必须 root 起 TUN daemon**
    （userspace 模式只能出站、收不到连入流量），而 brew 把服务以 root 起 → agent 的 `sudo`
    被禁就用不了（用户的 `开启远程访问.command` 也因此没能走完）。
  - **怎么拿到登录链接（可复用）**：`tailscale up` / `tailscale status` 在**非 TTY 下不打印 AuthURL**，
    `tailscale status --json` 里也没有这个字段。要从 daemon 直接取：
    ```bash
    curl -s --unix-socket /var/run/tailscaled.socket \
      http://local-tailscaled.sock/localapi/v0/status | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['AuthURL'])"
    ```
    返回形如 `https://login.tailscale.com/a/xxxxx`，直接给用户浏览器打开即可。
    （`tailscale status` 普通模式也会在 `NeedsLogin` 时打一行 `Log in at: <url>`。）
  - brew 会提示「Taking root:admin ownership of some tailscale paths … will require manual
    removal of these paths using sudo rm on brew upgrade/reinstall」→ **无害**，
    只是以后 `brew uninstall tailscale` 时要手动 `sudo rm` 清。

## 前端挂公网托管（2026-09-30 设计，**后端位置 10-02 改为计划上阿里云**）
原始架构：**前端静态挂 Vercel，后端在那台 Mac 上**，点开团请求打回后端，关掉页面任务仍继续
（本来就是服务端 `_job_thread`，这点不变）。搬云后规则不变，只是后端地址换成阿里云的。
**下面这些是公网部署的必要设计，搬云时要复用，不要删。**
- **后端必须开 CORS**：`app.add_middleware(CORSMiddleware, …)` **必须写在整个文件最后** ——
  Starlette 的 `user_middleware` 是 `insert(0)`，最后加的才是**最外层**；CORS 在最外层才能给
  「闸门返回的 401」补上 CORS 头，否则前端只看到不可读的跨域错误。
- **`is_local_request` 判据要看四样**：对端回环 + 无代理头 + Host 是回环主机名 + 没有跨站 Origin。
  只看前两样会把「Vercel 页面打过来的跨域请求」误判成本机。
- **`strict_access_key`（严格模式，`POST /api/access/strict`）**：开了连本机也要密钥。
  **挂隧道时必开** —— 反代可能把 Host 改写回 127.0.0.1，穿透 Host 白名单。
- **前端同一份 `index.html` 两种用法**：`API_BASE` 解析优先级 `?api=` > localStorage >
  `window.__QJL_API__` > 同源；`ACCESS_KEY` 同理 `?k=`。所有请求走 `apiFetch()` 自动补地址 +
  `X-Access-Key` 头（**跨域不能用 Cookie**，第三方 Cookie 会被浏览器拦）。
  打开方式：`<前端地址>/?api=https://<后端>&k=<密钥>`，带一次就记住。
- **前端发布目录 `deploy/`**（index.html + vercel.json，不含后端/数据/密钥）。
  改完前端要 `cp index.html deploy/index.html`。
- 必须是 **HTTPS 后端**：Vercel 页面是 https，http 会被当混内容拦掉。

## 网络
- `core.make_session()` 里设了 `s.trust_env = False` + 清空 proxies。
  原因：曾因继承会话级本地代理（`http_proxy=http://127.0.0.1:随机端口`）导致批量开团全部
  ProxyError 失败（那次白开 4 个、后续全挂）。要显式用代理只能通过 `QJL_PROXY`。
- 起服务前最好清掉 `*_proxy` 环境变量（`start.command` 已处理）。

## 命令约定
- **一律不要用原生 `alert`/`confirm`**：用户在嵌入式预览里操作，原生弹窗会被静默吞掉，
  表现为「点按钮没反应」。统一用页内弹窗 `uiAlert` / `uiConfirm`。

## 关键领域规则（血泪换来的）
- 广场混着两类活动：`followEarning` 非 null = 能开团；为 null = 不能开团（硬开必失败）。
  判据字段 `hasFollowRelation`。
- **`open_log` 写记录时必须同时写 `goodsIds` + `goodsNames`**。2026-09-23 修掉过一个 bug：
  `server.py` 算了 `names_by_act` 却从没写进日志 → 新开的团缺 goodsNames，
  5 天冷却只剩 actId/goodsId 命中，同款换供应商/改名就认不出来（表现为反复开同一款）。
- **批量详情接口 `query_list_by_act_ids` 是「整批同生共死」的**：批里一个无效 actId 会让
  整批返回 0 条（`[有效]`→1，`[有效,无效]`→0）。`core._fetch_details_batch` 已在整批失败时
  逐个重试救援。判断「接口没数据」之前先排除这一条。
- **工具看不到「你自己在群接龙里开的团」**：全库 `ghId == 自己账号` 的活动数 = 0，
  广场接口也没有自己的团列表（试过 10 个候选路径全 404），`foldActIds` 也恒为空。
  所以在工具外开的团不会进 open_log，冷却认不出来。两个补丁：
  ① 列表行的**「标记已开」**按钮（`POST /api/log/mark`）手动补录；
  ② **`isFollow`（已跟过）就是唯一能看到的「我自己开过」的证据** ——
     `parse_item` 输出 `alreadyFollowed`，前端开团面板**默认不勾选**这类行
     （仍然显示、仍然可勾）。实测一天 289 条里有 26 条属于这类。
- **用服务端 `keyword` 参数搜款号比搜商品名更准**：用户在群里贴的标题常带款号
  （如 `MY-0157`），直接 `keyword=MY-0157` 就能精确命中唯一那条源活动。
  搜商品名/供应商名也行，但注意**关键词搜索有它自己的窗口与上限**，要按需放宽日期。
- **`commissionPercent` 绝不能提交成 null**——平台会把整团佣金清零（历史 223 个团因此白开）。
- 拉取时**不要发 `firstCategoryIdList`**（会漏掉 60% 数据，实测 99 条 vs 256 条）。
- 同款判定（**2026-09-23 升级为三条判据**，见 `core._goods_groups`）：
  ① goodsId 有交集；② **全部规格**的商品名同款（完全相同 / 短名 ≥8 字被长名包含），
  且匹配到的名字要占少方 ≥50%（防「换购件共用」误并）；
  ③ **多规格价格指纹**：规格数相同 + 价格组合完全相同 + 名称集 Dice(2-gram) ≥0.45，
  两边都有款号且互不相交时否决。
  - 旧的 ①+②（且只用第一个商品名）会漏「标题被大改的跨供应商同款」——用户 2026-09-23 报的
    `.孟孟` vs `斗不斗®` 的 NOUSAKU 深睡睡衣就是（Dice 仅 0.52，最长公共片段只有 "NOUSAKU"）。
  - 标定依据：一周 2260 条真实数据，零回归，新增 14 对直接命中**全部真同款**，
    最近的误伤样本在 0.33（同品牌不同商品）。
- 「同款 5 天内已开」= 不丢弃、列表可见、但不开团（满 5 天自动恢复）。
  冷却判定会把**同款组里被合并掉的来源**的 goodsId/商品名也算进来
  （`_finish_fetch` 的 `merged_extra`）——去重后留下的那条未必是我开过的那条。

## 同款判定调参方法（可复用，不许拍脑袋定阈值）
拉一周全量真实数据（约 2260 条）→ 枚举全部候选对 → 按相似度降序**人工逐条核对** →
取「真同款最低分」与「误伤最高分」之间的**空档**作阈值 →
最后必须跑**回归**（确认新规则没丢掉旧规则能抓的每一对）。
那次结果：误伤 0.33、真同款最低 0.49，空档 [0.39,0.49] → 取 0.45。
核对清单已导出：`同款判定_新增识别清单.csv`。

## 图片相似度（结论：当前不可用）
- 详情页图文列表**能拿到**：`GET /activity-biz/help_sale_root/act/for_follow/{actId}/{ghId}`
  → `activityInfoDTO.activityDetail`（JSON 字符串，含 100+ 张图/视频路径）。1 次请求/活动。
- 但**图片本身下不到**：`img.qunjielong.com` / `cdn.qunjielong.com` → 139.224.174.130，
  TCP 80/443 全部超时（沙箱内外一样）；`apipro` 没有 `/up/` 静态路由（404）。
- 图片 URL 也不能直接比：每个团长会**重新上传**自己的图，路径完全不同
  （实测同一款睡衣，两边详情图 path 交集 = 0）。要比只能比像素（pHash）。

## Git / 公开仓库（2026-10-04，往 GitHub 推之前必读）
- 远端：`https://github.com/Harrydmyh/qjl.git`（SSH 形式 `git@github.com:Harrydmyh/qjl.git`）。
  ⚠️ **仓库是 PUBLIC（公开）** —— 往这里推任何东西都等于公开，**推之前先想清楚**。
- 本地分支已从 `master` 改名 **`main`**（对齐远端默认分支）；当时远端是**空的**（0 refs）。
- **`.gitignore`（现状）**：排除 `data/`（含 auth_token / gh_id / access_key —— **这一条必须留着**）、
  `__pycache__/`、`*.pyc`、`.env`、`.DS_Store`；`web/.gitignore` 另排除
  `node_modules/`、`dist/`、`.vercel/`。
  ✅ **按用户明确要求（2026-10-04）**：`.workbuddy/` 笔记 + 业务数据导出
  （`*开团记录核对.csv`、`同款判定_*.csv`、`待重开*`）**已纳入版本控制，会推到公开仓库**。
- ⚠️ **铁律：不要在 `.workbuddy/memory/` 里写任何密钥真值**（access_key / auth_token / gh_id / uid）——
  这些笔记会进**公开**仓库。2026-10-04 就发现 `2026-10-02.md` 抄了 access_key、`2026-10-04.md` 抄了 uid，
  已抹成「已隐去」。写笔记一律用占位符，或只写「见 `data/config.json`」。
- **真实账号标识曾硬写在 3 个文件**（`config.example.json`、`index.html`、`deploy/index.html`
  的 placeholder）→ 已用 `git filter-branch --tree-filter` **重写全历史**抹除；
  现在遍历全部提交扫 gh_id / uid / access_key / auth_token **0 命中**。
  → **以后新增示例值/占位符只能用假值**：现用 `PsXXXXXX_bsXXXXXX_xxxxxxxx` / `100000000`。
  （filter-branch 的替换脚本必须**幂等**，因为它会对每个提交各跑一次。）
- **推送凭证不在本机**：钥匙串里没有 GitHub 凭证；`~/.ssh/id_ed25519` 存在但从没加到 GitHub 账号
  （`ssh -T git@github.com` → Permission denied）。`gh` CLI **没装**。
  → 推送这一步**必须用户操作**（贴 SSH 公钥到 GitHub，或给 PAT）。
- **可复用的密钥安检**：提交树 `git grep -F <secret> HEAD`；
  全历史 `for c in $(git rev-list --all); do git grep -l -F <secret> $c; done`。

## 已废弃：本机常驻 + 内网穿透（2026-10-02 已清理）
`start.command`、`start-daemon.command`、`stop.command`、`stop.sh`、`开启远程访问.command`(Tailscale)、
`发布前端.command` + `~/Library/LaunchAgents/com.qjl.opener.plist` —— 全部移入废纸篓，
**只剩 `python3 server.py` 一条启动路径**（用户计划把服务搬阿里云）。
⚠️ `workbuddy_cloudstudio_deploy` 在这台机器上**连不上**（`fetch failed`，试过两次），别用它。

## 回答「做到哪一步」前必须先跑体检（2026-10-02 教训）
「服务在跑」≠「已常驻」，是三个独立状态，靠记忆答很容易误报：
1. `lsof -nP -iTCP:8765 -sTCP:LISTEN` —— 服务在不在（手动起的也算「在跑」）
2. `launchctl print gui/$(id -u)/com.qjl.opener` —— 有没有被 launchd 托管（**这才有开机自启**）
3. `brew services list | grep tailscale` + `ls /var/run/tailscaled.socket` —— Tailscale 起没起
判断跑的是不是最新代码：`ps` 在沙箱里被禁（`Operation not permitted`），别猜启动时间，
直接打新接口 —— `GET /api/access` 返回里有 `strict` 字段、`OPTIONS` 预检有 CORS 头 = 9-30 之后的代码。

## 数据文件
- `data/activities.json` 当前列表；`data/open_log.json` 开团记录（含 goodsIds/goodsNames，
  是 5 天冷却的唯一依据）；`data/config.json` 凭证（token / gh_id / access_key）。
- 改动前先备份；`activities.json.bak`、`open_log.json.bak_before_names` 是历史备份。

## 组件化前端 `web/`（2026-10-02 新增）
Vite + React + TypeScript，纯静态产物，给「前端挂 Vercel、后端常驻本机」这个架构用。
- 结构：`src/lib/api.ts`（后端地址/密钥/请求）、`src/lib/rules.ts`（业务判据纯函数）、
  `src/components/ActivityTable.tsx`（列表和开团面板共用）、`FetchView/OpenView/LogView/SettingsView`、
  `src/hooks/useJob.ts`（服务端任务状态 + 断线重连接回）。规则要改就改 `lib/rules.ts`。
- 命令：`npm run dev` / `npm run build`（先 `tsc --noEmit`）/ `npm run smoke`。
- `web/vercel.json` 就绪；Vercel 上 **Root Directory 设成 `web`**。
- ⚠️ **agent 环境里 `npm install` 会卡死**（环境有会话级 `http_proxy=http://127.0.0.1:随机端口`，
  npm 走它下 tarball 就挂：9 分钟零进展、缓存不增长）。
  必须 `env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY npm install` —— 19 秒就装完。
- ⚠️ **jsdom 不支持 ES module**，冒烟测试要先用 esbuild 打 IIFE 再注入。
- ⚠️ 给 fetch 加模拟头**别写 `{...init.headers}`**：`Headers` 对象展开会变空对象，把
  `X-Access-Key` 丢掉（冒烟测试里踩过，导致跨域用例假失败）。
- ⚠️ `smoke.mjs` 一开始会在 8765 上留垃圾进程；已改成「已有服务就复用、绝不杀；自己起的必须等端口释放再退」。
