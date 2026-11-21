# 项目长期注意事项（开团助手 / 群接龙）

## 运行
- 启动 `/opt/homebrew/bin/python3 server.py`，端口 8765。**只有 Homebrew 的 python3 装了 fastapi** —— PATH 里默认的 workbuddy 3.13 和 /usr/bin/python3 都没有，直接 `python3 server.py` 会 ModuleNotFoundError。（2026-11-14 又踩一次）
- 页面每次从磁盘读 → **改前端不重启，改 core/server 必须重启**。后台起只能 start_new_session=True。
- 停服务别用 `pkill -f server.py`（误杀工具自身）：`lsof -nP -iTCP:8765 -sTCP:LISTEN -t | xargs kill`。
- 用户报 Failed to fetch → 先 lsof，没有=服务没起。常驻/launchd 已全删。
- **自动开团已内置到服务**（2026-11-14）：`/auto` 页面 + `auto_runner.py` 后台线程（20 秒 tick，默认关，开关在 `data/auto_state.json`）。开团账号独立存 `data/open_account.json`，不动 config.json。改 server/auto_runner 必须重启服务。
- 当服务器必须插电；合盖必睡 → `sudo pmset -a disablesleep 1`（恢复 0）。
- **有两台机器、两套副本（2026-11-21 查明）**：用户平时点「生成」的 `http://150.136.15.0:8765/auto?k=…` 是**云服务器**，不是这台 Mac；两台各有独立的 `data/`（快照/open_plan/sched_* 都不同步），只共享 git。云上无 SSH 权限（root/ubuntu/opc 全 publickey denied），拉代码要用户手动。约定：每周只在 22:00~07:00 各跑一次、出表前两台都 `git pull`（周报/ 是排期记忆来源）、都跑了只用先出的那份。

## 工作流（用户偏好）
- 改完**不截图**，`present_files` 开预览 `http://127.0.0.1:8765/`；手机效果受面板宽度影响（>768px 是桌面表格）。
- pull → 改 → 提交 →（他说推才）push。**拉取前先把 `.workbuddy/` 未提交笔记 stash**。
- **不用原生 alert/confirm**（预览里被吞）：用页内 uiAlert/uiConfirm。Edit 改完 grep 验落盘。

## 网络
- `core.make_session()` `trust_env=False`+清 proxies；代理只走 `QJL_PROXY`；起服务前清 `*_proxy`。

## 公网部署资产（搬阿里云复用，别删代码）
- 密钥闸门 `config.json` 的 `access_key`：本机免密，外部来源带 cookie/`?k=`/`X-Access-Key`；有反代必开 `strict_access_key`。
- **CORS 中间件必须写在文件最后**（Starlette insert(0)），否则 401 无 CORS 头。is_local_request 看四样：对端回环+无代理头+Host 回环+无跨站 Origin。
- `deploy/` 是发布目录（改完同步过去），后端必须 HTTPS。
- 上云前先修：任务状态只在内存，重启跑丢。
- 活动链接：`https://www.qunjielong.com/#/dashboard/{actId}`（网页版，需登录）。

## 关键领域规则
- `followEarning` 非 null = 能开团；null = 不能开。
- 帮卖两套模式：跟团 `for_follow/{actId}/{ghId}`+`/act/release`（core.open_one_group）；**子团 `for_leader/…`+`help_sale_child/act/release`（qjl_auto.open_child，已实测可用）**，activityType **10→5830，150/160/210 可开**。新号 uid155475458 只能走子团，候选来自 `ghome-feed/act_recommend/get_recommend_acts`。
- **两个汤团号（同一 token 都能访问）**：`PspU4o9_bsxkBH0_6c6cef00`=汤团（用来开团）、`PsHHJN8_bsMtSbS_b09f0984`=汤团²（data/config.json 里配的，拉帮卖广场数据用它）。用户说"汤团"指的是 PspU4o9。
- **判"这号开过没"别只看 isFollow**：帮卖 234 条里 202 条 isFollow=True，但主页真正还有在售同名团的只有 56 条。正确判据 = 拉自己主页 feed（`query_ghome_feed_back_v2`，ghType=20）按**归一化活动名前 40 字匹配 + activityStatus=10 + pauseSw=5**。脚本 check_mine.py / verify_bs_tang.py。
- **跟团/帮卖只能按 `activityType` 分**（10=跟团、150/160/210=帮卖子团），**不能按 ghType**：ghType 10=品牌号(自营，朱雅琪/新物商店/淼妈家=跟团)、20=团长号(明星团长露西Grocery/明星品牌馆/绿头像=帮卖)、105=供货品牌(骄傲美食/禧牛/YS供应链=帮卖)。详情接口 `upStreamType` 恒 None，不可用。
  请求体两坑：activitySettingsParam 用成功样本补齐（缺 showAllGroupOrder 报 5259）；手机号带 `?ghId=` 打 get_user_input → inputMap["210"]。
- open_log 必须同时写 goodsIds+goodsNames；commissionPercent 绝不能为 null（整团佣金清零）；拉取别发 firstCategoryIdList。
- 批量详情 query_list_by_act_ids 整批同生共死（一个无效 actId 整批返 0，已加逐个重试）；keyword 搜款号更准。
- **批量详情还会偶发串号**：同一 actId 批量查 8230、单查 227。凡是要拿订单数做门槛/报表的，必须**逐条查**（357 条约 2 分钟），批量结果发现"订单数骤降"先怀疑串号、别当退款。
- **`extraParam.from` 是入口级可见性开关（2026-11-14 踩过）**：`query_list_by_act_ids` 传 `from:6` 会对整整 20 家团长（家乡美/屿憩/淘天下/恰食橙呈/ATM品牌团/炸弹天团…）返回空数组且 code=200，看起来像"已下架/合作取消"，其实活动还在卖。**用 `from:8`（是 6 的超集），空再试 6/9/11**。判"某团没了"之前先换 from 复核。
- 千单/周报的模式口径（2026-11-14 复核确认）：**只看 `activityType`** —— 10=跟团、150/160/210=帮卖。ghType 只作账号旁注（10=品牌自营号、20=选品/明星团长号、105=供货品牌号），**不能用来分组**（早期按 ghType 分组被用户当场纠正过两次）。feed 的 `upStreamTypeList=[2]`↔ghType10、`[3]`↔ghType20|105 可交叉验证。
- 平台**没有**"某活动最近下单时间"接口（`get_last_order_by_act_id` 各前缀 404、detail 无 endTime）。判"近期还在出单"只能：①两次快照比 totalOrderNum（21 分钟窗口命中率仅 4%；且逐条查才会稳定，批量会乱）；②与旧报表对比总单量，仅凭"累计单量没变"剔死团。
- 看不到自己开的团 →「标记已开」/api/log/mark 补录；isFollow→alreadyFollowed 默认不勾选。
- 同款判定三条判据：goodsId 交集；全部规格商品名同款（匹配名占少方≥50%）；多规格价格指纹（Dice≥0.45）。阈值 0.45，调参必跑回归。
- 「同款 5 天内已开」= 不丢弃、可见、不开团；图片相似度暂不可用。
- **供货商黑名单（永久）**：`data/blacklist_suppliers.json` 的 `suppliers` 里名字**子串命中 ghName** 就整家不开团（2026-11-14 用户点名：简小姐、悦宝）。weekly800.py 过滤在门槛后/季节前，单列「黑名单剔除」页；auto_open_hourly 也拦一道。加人只改这个 json，不要写死在别处。
- **开团查重（2026-11-14 定稿）**：5 天内开过同款不开 + 两份表之间重复只开一条。证据源 = `list_gh_child_activities(pactId=母团, ghId=汤团)` 的子团 createTime，+ 主页近 5 天团名。**不能按「有在售子团」判**——该号每 7~20 天就把同一母团重开一次，620 条里 371 条都有在售子团，按在售判会一条都开不出来。名字匹配必须用 **actName**（活动标题），不能用 goodsName：两者差很远。`dedup_open.py` 出结论，`auto_open_hourly.precheck()` 开前现场复核。

## 周报排期 + 自动开团（2026-11-14）
- 出表 `weekly800.py`（--offline 只用当日详情缓存）；排期 `split_seven`：只在"要开的"里 7 天均分，贪心塞进「同类最少+同家最少」那天，日内按 食品→日用→服饰美妆→杂货 轮转且相邻不同供货商。
- 四分类 `classify_cat.py`（DeepSeek，缓存 data/cat_cache.json；1食品/2日用/3服饰美妆/4杂货，判不出归 4）。
- **稳定排期（同一条链接尽量排回上周同一天）的匹配键**：**actId 稳定、链接每周重新生成**。`short_link_cache.json` 存的是最新链接，可反查 actId。`load_actual_plan()` 读最新的 `复团*.xlsx`（只有链接、没有活动ID）→ 反查 actId，优先级仅低于 `sched_override.json`。只按 actId 读周报会让这批整批"失忆"被重排。
- **同一天的 11-14 可能有两份不同计划**（`复团1114` 12:13 vs 周报 17:15，113 条周几不同），用户核对的是**实际执行的那份复团表**。
- **表里的日期是"今天起 7 天"**（旧 next_week_dates 排下周，周六出的表当天用不上，已改）。
- **出表时点铁律（2026-11-21 用户定）**：每周**只在「周五 22:00 之后 ~ 周六 07:00 之前」跑一次，绝不跑第二次**。原因：当天剩余开团位决定排期是否被顺延（06:30=61 位、07:30=58 临界、08:00=56 会挤掉 1~2 条、10:00=48）；20 小时内重复跑会判定"上一份快照太新"→ 回落 7 天前基线，"近期有出单"变周对周、多剔一批。**实际执行方式（2026-11-21 18:20 定）**：用户自己在云服务器 `http://150.136.15.0:8765/auto?k=…` 点「生成」跑，**只跑那一边、窗口内只跑一次**；本机 automation `f63f64d0`（原定周六 06:30）已 **PAUSED**，避免两边各出一份。云端必须 `git pull` 到最新才有 20 小时防重复。
- 执行：`auto_open_hourly.py` 读 `data/open_plan_tang.json`（weekly800 每次出表自动重写，保留已 opened 状态）；7-22 点、每波最多 4 条、条间随机 20-50s；`data/open_plan_STOP` 停；token 过期自停。
- 对应 automation：汤团帮卖·每小时自动开团（FREQ=HOURLY）。**rrule 的 BYHOUR 只能单值**，所以不能"每天 7-22 每小时"，只能整点跑 + 脚本内判窗口。

## token
- JWT HS512 能读能验、签不出续不了；payload 只有 `uid`+`exp`、无 iat 无 refresh。微信登录不能脚本化（一次性 code）→ 上限是 mitmproxy 捞 token 写回配置。
- **唯一续期入口** `POST /user/login/wx_login_silence`，body `{code, scene:1005, appid:wx059cd327295ab444}`，要 wx.login 的一次性 code → 脚本复刻不了。全站只有它会吐 token，响应头无 set-token。
- **token 失效判据**：任何接口返回 `{"code":403,"msg":"登录凭证不合法或已过期"}`（严格校验、无宽限，过期 3 秒就 403）。脚本命中就该自停/告警。
- 有效期实测（2026-11-20 抓包）：本轮两枚 token 都是签发后**整 7 天**；早期记的"48 小时"未复现，可能因登录入口不同，别当硬结论。

## 售后（after_sale_probe.py）
- 列表 POST /after-sales-biz/as_order_search/v2/search。ghViewType 10=别人向我发起（要这个）/20=我向上游，互斥；asStatusList=[10,20] 待处理，[] 会拉到 2024 年归档；searchContent 只认手机号/订单号，售后单号用 asOrderNo。
- 「复制信息」无后端（handleCopyOrderData 零请求）；asContent 大量为空必须容错；价格单位分。

## 合集（act_collection，2026-11-05 实盘）
- 新建 `act_collection_create`（**body 带 actCollectionId 也照样新建**）；原地改只能 `act_collection_modify`（保住已分享链接）；`actIdList: []` 被忽略，无删除接口。
- 上限 200 条；选团列表复用 query_ghome_feed_back_v2（无限流，按 createTime 边界停，置顶+时间倒序）。
- per-act「近 7 天有单」无接口；`get_last_order_by_act_id?actId=`（query 参数）只给累计第 N 单。
- 详见 2026-11-05.md；脚本 fetch_feed_7d.py + collection_fill.py。

## 汤团「日间集市」每日合集（daily_collection.py，2026-11-21 起）
- 每天 19:00 建一条 `汤团日间集市M.D`，条数浮动 80~200，封面 6 款必须食品；automation `d343332a`。
- **商品数据接口** `POST /ghdata-biz/api/ghData/queryPageGhGoodsRank`：`dataType=10` 是今天，`orderField=uv|buyQuantity`，**翻页用 `page`（写 pageNum 无效）**，每页固定 30。销量榜几页就到底＝当天有单商品全量（实测覆盖 147 单 vs 大盘 149）；访问榜只取前 30 页按 35% 截。`dateTag` 传什么都一样，不能用来切日期。
- **自己开的团查不到「帮卖广场详情」**（query_list_by_act_ids 对自家团返空）；只有 `POST /activity-detail/activity/detail/general_goods {activityId}` 能拿到 goodsId 列表。
- 主页 feed 的 `actItemDTO.ghId` 恒等于汤团自己（子团都是它开的），不能用来判「谁开的」；`boughtCount` 与 `goodsAllOrderNum` 都不是本团销量，别当今日单量用。
- 权重：有单 100 > 访问前35% 90 > 补位 10；「今天开的团」只做补位，给高分会把 200 个位置全占满、把真出单的挤掉。

## 抓包反推
.chlz=zip（meta/req/res 三个 json、body 已明文）→ 用用户级 skill reverse-engineer-app-capture。

## Git / 公开仓库
- origin `git@github.com:Harrydmyh/qjl.git`，**PUBLIC，推=公开**。data/ 被 .gitignore 排除（必须留）。
- **铁律：memory 绝不写密钥真值**（只写「见 data/config.json」）；**示例值只用假值** `PsXXXXXX_bsXXXXXX_xxxxxxxx`/`100000000`（曾 filter-branch 抹除）。
- 推送用 ~/.ssh/id_ed25519（Harrydmyh）；Harrydm5 只有 pull。
- 数据文件：data/activities.json、open_log.json、config.json，先备份。

## 前端 web/（Vite+React+TS）
- 规则改 src/lib/rules.ts；npm run dev|build|smoke；Vercel Root=web。
- agent 环境 npm install 卡死 → `env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY npm install`。
- jsdom 不支持 ESM，冒烟用 esbuild 打 IIFE；fetch 加头别写 `{...init.headers}`。

## index.html 手机端
- 窄屏表格→卡片；表头全选/排序入口消失 → 已加 .list-bar 选择条（#mSelectAll/#sortSel）。供应商排序必须 localeCompare(b,'zh-Hans-CN')。
