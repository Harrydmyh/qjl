# 项目长期注意事项（开团助手 / 群接龙）

## 运行方式（2026-10-02 清理后：只有一条路）
- 启动：`cd /Users/apple/noah-chat/qjl && python3 server.py`；停：该终端 `Ctrl+C`。
- 系统 Python `/opt/homebrew/bin/python3`（managed 3.13.12 **没装 fastapi**）。
- 监听 `http://127.0.0.1:8765`；页面每次请求从磁盘读 `index.html`（`no-store`）
  → **改前端不用重启**；改 `core.py`/`server.py` **必须重启**。
- 别用 `nohup &` / run_in_background 起服务（活不过那次调用）。后台起只能
  `start_new_session=True`。停：**不要用 `pkill -f server.py`**（会误杀工具自身），
  用 `lsof -nP -iTCP:8765 -sTCP:LISTEN -t | xargs kill`。
- ⚠️ 用户点「刷新列表」报 `Failed to fetch` → **先 `lsof` 查端口**，没有就是服务没起，别查代码/跨域。
- **已全部删进废纸篓** `~/.Trash/qjl-清理-2026-10-02/`（可恢复）：`start.command`、
  `start-daemon.command`、`stop.command`、`stop.sh`、`发布前端.command`、
  `开启远程访问.command`、`~/Library/LaunchAgents/com.qjl.opener.plist`。
  launchd 常驻已卸 → **重启机器不会自启**。用户计划搬阿里云，**别再加本机常驻/穿透/双击脚本**。

## 体检：回答「现在什么状态」前必跑（别凭记忆）
三个独立状态，**「服务在跑」≠「已常驻」**（这点误判过）：
1. `lsof -nP -iTCP:8765 -sTCP:LISTEN` —— 在不在跑
2. `launchctl print gui/$(id -u)/com.qjl.opener | grep -E "state|pid|properties|runs"` —— 有没有被托管
3. `tailscale status` + `ls /var/run/tailscaled.socket` —— 隧道起没起

判断跑的是不是最新代码：`ps` 在沙箱被禁，**别猜启动时间**，直接打接口 ——
`GET /api/access` 有 `strict` 字段 + `OPTIONS` 预检有 CORS 头 = 9-30 之后的代码。
验证崩溃自愈：`launchctl kickstart -k gui/$(id -u)/com.qjl.opener`（`runs` +1、pid 变新）。
⚠️ agent 跑 `launchctl bootstrap` 必报 `Bootstrap failed: 5: Input/output error`（关沙箱也一样，
plist 合法）—— 注册只能在用户交互会话里做；但 `print`/`kickstart`/`bootout` 都能用。

## 电源 / 合盖（MacBook Pro M3、macOS 15.7.2、只有内建屏）
- AC `sleep 0`（插电永不睡）、`displaysleep 0`；Battery `sleep 20` 分钟 → **当服务器要插电**。
- **合盖必睡**（无外接屏，插电也一样）→ 想合盖跑：`sudo pmset -a disablesleep 1`（恢复 `0`）。
  ⚠️ 合盖散热差，放硬面。备选：HDMI 欺骗器（约 ¥20，原生 clamshell 模式）。
- **判断 pmset 认不认某个键（可复用）**：跑 `pmset -a <key> 1` 看报错 ——
  有效 → `'pmset' must be run as root`；无效 → `Usage: pmset <options>`。
  （`man pmset` 里**没有** disablesleep 条目，别靠 man 判断。）
- 熄屏无影响；系统睡眠会冻结进程（醒来接着跑，但正在发的那一条会失败）。

## 改动 / 评审工作流（用户偏好，2026-10-07 明确）
- **改完不要截图**，直接 `present_files` 打开内置预览 `http://127.0.0.1:8765/` 让用户自己看。
- ⚠️ **手机效果受预览面板宽度影响**：`@media (max-width:768px)` 才切卡片 → 面板宽于 768px
  看到的是桌面表格。提醒用户把面板拖窄。
- 用户在别处改并 push，我是 `git pull` → 改 → 提交 →（他说推才）push。
  **拉取前先把 `.workbuddy/` 未提交笔记 `git stash`**，否则挡住 fast-forward。

## 公网部署资产（搬阿里云要复用，**别删代码**）
用户计划：前端静态托管 + 后端搬阿里云。下面两块是已做好的必需品：
- **访问密钥闸门**：`config.json` 的 `access_key`（设了才生效）。本机直连免密钥，
  其它来源必须带 cookie / `?k=` / `X-Access-Key`（带一次自动种 cookie 180 天）。
  接口 `GET /api/access`、`POST /api/access/rotate|disable`。`/api/config` 会过滤掉该键。
  不用 HTTP Basic（嵌入预览里原生弹窗会被吞）。现状 `access_key` 仍启用；
  **上云建议开 `strict_access_key`**（有反代时必开：反代可能把 Host 改回 127.0.0.1 绕过白名单）。
- **CORS**：`app.add_middleware(CORSMiddleware, …)` **必须写在文件最后**（Starlette
  是 `insert(0)`，最后加的才在最外层），否则 401 没有 CORS 头、前端只看到不可读错误。
  `is_local_request` 要看**四样**：对端回环 + 无代理头 + Host 回环 + 无跨站 Origin。
  前端 `?api=`/`X-Access-Key` 跨域方案已在（跨域不能用 Cookie，第三方 Cookie 会被拦）。
  `deploy/` 是发布目录（改完要 `cp index.html deploy/index.html`）。后端必须 HTTPS。
- 网络事实：内网 `10.88.49.26`、出口 `18.136.28.35`（新加坡 AWS），**NAT 后无公网 IP**。
- 上云前建议先修：**开团任务状态只存在内存里**，进程重启会跑丢。

## 网络
- `core.make_session()` 设了 `trust_env=False` + 清空 proxies（曾因继承会话级代理导致批量开团
  全部 ProxyError）。要代理只能走 `QJL_PROXY`。起服务前清 `*_proxy`。

## 命令约定
- **一律不用原生 `alert`/`confirm`**：嵌入式预览里会被静默吞掉，表现为「点按钮没反应」。
  用页内 `uiAlert` / `uiConfirm`。

## 关键领域规则（血泪换来的）
- 广场两类活动：`followEarning` 非 null = 能开团；null = 不能开（硬开必失败）。判据 `hasFollowRelation`。
- **`open_log` 必须同时写 `goodsIds` + `goodsNames`**（2026-09-23 修过 bug：算了却没写 →
  冷却只剩 actId/goodsId 命中，换供应商/改名就认不出，表现为反复开同一款）。
- **`commissionPercent` 绝不能提交成 null** —— 平台会把整团佣金清零（历史 223 个团白开）。
- 拉取**不要发 `firstCategoryIdList`**（漏 60% 数据，实测 99 vs 256）。
- **批量详情 `query_list_by_act_ids`「整批同生共死」**：批里一个无效 actId 让整批返回 0
  （`[有效]`→1，`[有效,无效]`→0）。已在整批失败时逐个重试救援。
- **服务端 `keyword` 搜款号比搜商品名准**（如 `MY-0157` 精确命中唯一源活动）。
- **工具看不到你自己开的团**：全库 `ghId == 自己账号` = 0，无自己的团列表接口（10 个候选全 404），
  `foldActIds` 恒空。补丁：① 列表行「标记已开」(`POST /api/log/mark`) 手动补录；
  ② `isFollow`（已跟过）→ `alreadyFollowed`，开团面板**默认不勾选**（仍可勾）。一天 289 条中 26 条属此类。
- **同款判定三条判据**（`core._goods_groups`）：① goodsId 交集；② **全部规格**商品名同款
  （完全相同/短名≥8字被长名包含），且匹配名占少方 ≥50%（防换购件误并）；
  ③ **多规格价格指纹**：规格数同 + 价格组合全同 + 名称集 Dice(2-gram) ≥0.45，两边都有款号
  且互不相交时否决。旧规则（只用第一个商品名）会漏标题被大改的跨供应商同款。
- 「同款 5 天内已开」= 不丢弃、列表可见、但不开团（满 5 天自动恢复）。冷却判定会把
  **同款组里被合并掉的来源**的 goodsId/商品名也算进来（去重后留下的未必是我开过的那条）。

## 同款判定调参方法（可复用，不许拍脑袋）
拉一周全量（约 2260 条）→ 枚举候选对 → 按相似度降序**人工逐条核对** →
取「真同款最低分」与「误伤最高分」的空档作阈值 → **必须跑回归**（确认没丢旧规则能抓的）。
那次：误伤 0.33、真同款最低 0.49 → 取 0.45。清单 `同款判定_新增识别清单.csv`。

## 图片相似度（当前不可用）
图文列表能拿：`GET /activity-biz/help_sale_root/act/for_follow/{actId}/{ghId}` →
`activityInfoDTO.activityDetail`。但**图下不到**（img/cdn.qunjielong.com → 139.224.174.130，
TCP 80/443 全超时；apipro 无 `/up/` 路由）。且每个团长会重传图（同款两边 path 交集 = 0），
只能比像素 pHash。

## token（2026-10-07 查证）
- JWT `HS512`，payload 明文 `{"uid":164779047,"exp":1791506504}` → **能读能验，签不出也续不了**。
  抓包 431 个请求里**没有任何 refresh/login/code2session 接口**，无 refresh_token。
- **有效期固定 48 小时**，到期只能重新登录小程序抓（用户目前约每 2 天手动一次）。
- 自动化的边界：**微信登录那两步不能脚本化**（code 只能由真实微信客户端产生、一次性、绑本人会话；
  伪造违反协议且必撞风控）→ 只能做到「mitmproxy 无头脚本从流量里捞 token 写回配置」。
- 我们请求头与真实小程序有差异（client-version 6.2.77 vs 6.2.83、scenecode 1008 vs 1005、
  mini-route 多了值），目前不影响，行为异常时是第一手对照材料。

## Git / 公开仓库（往 GitHub 推之前必读）
- 远端 `git@github.com/Harrydmyh/qjl.git`。⚠️ **PUBLIC 公开仓库**，推任何东西 = 公开。
- 分支 `main`。`.gitignore` 排除：`data/`（含 auth_token/gh_id/access_key，**必须留**）、
  `__pycache__`、`*.pyc`、`.env`、`.DS_Store`；`web/.gitignore` 排除 node_modules/dist/.vercel。
  ✅ 按用户要求：`.workbuddy/` 笔记 + 业务 CSV **已纳入版本控制（会公开）**。
- ⚠️ **铁律：`.workbuddy/memory/` 里绝不能写密钥真值**（access_key/auth_token/gh_id/uid）。
  2026-10-04 发现两处抄了真值，已抹。写笔记用占位符或只写「见 data/config.json」。
- 真实账号标识曾硬写在 `config.example.json`/`index.html`/`deploy/index.html` → 已用
  `git filter-branch --tree-filter` **重写全历史**抹除（替换脚本必须**幂等**）。
  **以后示例值只能用假值**：`PsXXXXXX_bsXXXXXX_xxxxxxxx` / `100000000`。
- 推送：本机 `~/.ssh/id_ed25519`（`SHA256:1wOKow…`）已加进 **Harrydmyh**，SSH 推送正常。
  ⚠️ 用户有两个账号：`Harrydmyh`（所有者，SSH 已通）/ `Harrydm5`
  （`~/.git-credentials` 里 40 位明文 token 所属，对本项目**只有 pull**，建议删换）。`gh` CLI 没装。
- 密钥安检：提交树 `git grep -F <secret> HEAD`；全历史
  `for c in $(git rev-list --all); do git grep -l -F <secret> $c; done`。

## 数据文件
- `data/activities.json` 当前列表；`data/open_log.json` 开团记录（5 天冷却唯一依据）；
  `data/config.json` 凭证。改动前先备份。

## 组件化前端 `web/`（2026-10-02 新增）
Vite + React + TS 纯静态，给「前端托管 + 后端本机/云」用。
- `src/lib/api.ts`（地址/密钥/请求）、`src/lib/rules.ts`（判据纯函数）、
  `components/ActivityTable.tsx`（列表与开团面板共用）、`hooks/useJob.ts`（任务状态+断线重连）。
  规则改 `lib/rules.ts`。`npm run dev|build|smoke`。Vercel **Root Directory 设 `web`**。
- ⚠️ agent 环境 `npm install` 会卡死（会话级 `http_proxy`），必须
  `env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY npm install`（19 秒装完）。
- ⚠️ jsdom 不支持 ESM，冒烟测试先用 esbuild 打 IIFE。
- ⚠️ 给 fetch 加头**别写 `{...init.headers}`**（`Headers` 展开变空，会丢 `X-Access-Key`）。
- ⚠️ `smoke.mjs` 曾留垃圾进程；已改成「已有服务就复用绝不杀，自己起的必须等端口释放」。

## 前端（index.html）手机端
- 窄屏把 8 列表格改成**卡片**（`thead{display:none}`，用 order 重排 td）。
  ⚠️ 由此派生：**表头里的全选 `#selectAll` 和排序入口在手机上全消失** → 已加
  `.list-bar`「选择条」（`#mSelectAll`/`#mSelCount`/清除/`#sortSel`/`#sortDirBtn`；开团面板
  `#mOpenAll`/`#mOpenCount`），`updateSelUI()`/`updateOpenStat()` 双向同步。
- **按供应商排序**必须用 `localeCompare(b,'zh-Hans-CN')`（`<`/`>` 是 UTF-16 码点序，中文不对），
  同家内再按 totalOrders 降序；文本类默认升序、数值/时间类默认降序。
- ⚠️ **Edit 报 success 也可能没落盘**（old_string 前导空格与文件不符时遇到过）→
  改完 CSS/HTML 用 grep 确认真的写进去了。
