# 项目长期注意事项（开团助手 / 群接龙）

## 运行
- 启动 `/opt/homebrew/bin/python3 server.py`，端口 8765。**只有 Homebrew 的 python3 装了 fastapi**，PATH 默认的 python 与 /usr/bin/python3 都没有 → 必须写绝对路径。
- 页面每次从磁盘读 → 改前端不重启，改 core/server 必须重启。后台起用 `start_new_session=True`。
- 停服务：`lsof -nP -iTCP:8765 -sTCP:LISTEN -t | xargs kill`。**别用 `pkill -f server.py`**（误杀工具自身）。
- 报 Failed to fetch → 先 lsof，空=服务没起。常驻/launchd 已全删。
- 自动开团内置在服务里：`/auto` 页面 + `auto_runner.py` 后台线程（20s tick，默认关，开关 `data/auto_state.json`）；开团账号独立存 `data/open_account.json`，不动 config.json。
- 当服务器必须插电；合盖必睡 → `sudo pmset -a disablesleep 1`（恢复 0）。
- **两台机器两套副本**：用户点「生成」的 `http://150.136.15.0:8765/auto?k=…` 是**云服务器**，不是这台 Mac；两边 `data/` 独立不同步，只共享 git；云端无 SSH 权限，拉代码要用户手动。

## 工作流（用户偏好）
- 改完**不截图**，`present_files` 开预览 `http://127.0.0.1:8765/`；>768px 是桌面表格。
- pull → 改 → 提交 →（他说推才）push；拉取前先把 `.workbuddy/` 未提交笔记 stash。
- **不用原生 alert/confirm**（预览里被吞），用页内 uiAlert/uiConfirm；Edit 改完 grep 验落盘。

## 网络
- `core.make_session()` `trust_env=False` + 清 proxies；代理只走 `QJL_PROXY`；起服务前清 `*_proxy`。

## 公网部署（搬阿里云复用，别删代码）
- 密钥闸门 `config.json` 的 `access_key`：本机免密，外部带 cookie/`?k=`/`X-Access-Key`；有反代必开 `strict_access_key`。
- **CORS 中间件必须写在文件最后**（Starlette insert(0)），否则 401 无 CORS 头。is_local_request 看四样：对端回环+无代理头+Host 回环+无跨站 Origin。
- `deploy/` 是发布目录（改完同步过去），后端必须 HTTPS。上云前先修：任务状态只在内存，重启跑丢。
- 活动链接：`https://www.qunjielong.com/#/dashboard/{actId}`（网页版，需登录）。

## 关键领域规则
- `followEarning` 非 null = 能开团。
- 帮卖两套模式：跟团 `for_follow/{actId}/{ghId}`+`/act/release`（core.open_one_group）；**子团 `for_leader/…`+`help_sale_child/act/release`（qjl_auto.open_child）**，activityType **10→5830，150/160/210 可开**。uid155475458 只能走子团，候选来自 `ghome-feed/act_recommend/get_recommend_acts`。
- **两个汤团号（同一 token 都能访问）**：`PspU4o9_bsxkBH0_6c6cef00`=汤团（开团用）、`PsHHJN8_bsMtSbS_b09f0984`=汤团²（config.json 配的）。用户说"汤团"= PspU4o9。
- **跟团/帮卖只能按 `activityType` 分**（10=跟团、150/160/210=帮卖子团），**不能按 ghType 分组**（ghType 只作旁注：10=品牌自营号、20=选品/明星团长号、105=供货品牌号）——早期按 ghType 分组被用户当场纠正两次。feed 的 `upStreamTypeList=[2]`↔ghType10、`[3]`↔ghType20|105 可交叉验证。详情接口 `upStreamType` 恒 None，不可用。
- 请求体两坑：activitySettingsParam 用成功样本补齐（缺 showAllGroupOrder 报 5259）；手机号带 `?ghId=` 打 get_user_input → inputMap["210"]。
- open_log 必须同时写 goodsIds+goodsNames；commissionPercent 绝不能为 null（整团佣金清零）；拉取别发 firstCategoryIdList。
- 批量详情 `query_list_by_act_ids`：整批同生共死（一个无效 actId 整批返 0，已加逐个重试）；keyword 搜款号更准。
- **批量详情会偶发串号**（同一 actId 批量 8230、单查 227）。凡要拿订单数做门槛/报表，必须**逐条查**（357 条约 2 分钟）；看到"订单数骤降"先怀疑串号，别当退款。
- **`extraParam.from` 是入口级可见性开关**：`from:6` 会对 20 家团长（家乡美/屿憩/淘天下/恰食橙呈/ATM品牌团/炸弹天团…）返回空数组且 code=200，像"已下架"其实还在卖。**用 `from:8`**（6 的超集），空再试 6/9/11。
- 平台**没有**"某活动最近下单时间"接口。判"近期还在出单"只能：①两次快照比 totalOrderNum（21 分钟窗口命中率仅 4%，且逐条查才稳）；②与旧报表比总单量。仅凭"累计单量没变"剔死团很危险。
- 看不到自己开的团 →「标记已开」`/api/log/mark` 补录；isFollow→alreadyFollowed 默认不勾选。
- 同款判定三条判据：goodsId 交集；全部规格商品名同款（匹配名占少方≥50%）；多规格价格指纹 Dice≥0.45。阈值 0.45，调参必跑回归。「同款 5 天内已开」= 不丢弃、可见、不开团。
- **供货商黑名单（永久）**：`data/blacklist_suppliers.json` 的 `suppliers` 名字**子串命中 ghName** 就整家不开（2026-11-14 点名：简小姐、悦宝）。weekly800 过滤在门槛后/季节前，单列「黑名单剔除」页；auto_open_hourly 也拦一道。加人只改 json。
- **开团查重（2026-11-14 定稿）**：5 天内开过同款不开 + 两份表重复只开一条。证据源 = `list_gh_child_activities(pactId=母团, ghId=汤团)` 子团 createTime + 主页近 5 天团名。**不能按「有在售子团」判**——该号每 7~20 天重开同一母团，620 条里 371 条有在售子团，按在售判一条都开不出。名字匹配必须用 **actName**，不能用 goodsName。`dedup_open.py` 出结论，`auto_open_hourly.precheck()` 开前现场复核。

## 周报排期 + 自动开团
- 出表 `weekly800.py`（--offline 只用当日详情缓存）；排期 `split_seven`：只在"要开的"里 7 天均分，贪心塞进「同类最少+同家最少」那天，日内按 食品→日用→服饰美妆→杂货 轮转且相邻不同供货商。
- 四分类 `classify_cat.py`（DeepSeek，缓存 `data/cat_cache.json`；1食品/2日用/3服饰美妆/4杂货，判不出归 4）。
- **稳定排期的匹配键：actId 稳定、链接每周重新生成**。`short_link_cache.json` 存最新链接可反查 actId；`load_actual_plan()` 读最新 `复团*.xlsx`（只有链接）→ 反查 actId，优先级仅低于 `sched_override.json`。只按 actId 读周报会让这批"失忆"被重排。同一天可能有两份不同计划，用户核对的是**实际执行的复团表**。
- **表里的日期是"今天起 7 天"**（旧 next_week_dates 排下周已废弃）。
- **出表时点铁律（2026-11-21 用户定）**：每周**只在「周五 22:00 ~ 周六 07:00」跑一次，绝不跑第二次**。原因：当天剩余开团位决定排期是否顺延（06:30=61 位、07:30=58 临界、08:00=56 挤掉 1~2 条、10:00=48）；20 小时内重复跑会判"快照太新"→ 回落 7 天前基线，多剔一批。**执行方式**：用户自己在云服务器点「生成」，**只跑那边、窗口内只跑一次**；本机 automation `f63f64d0` 已 PAUSED。
- 执行：`auto_open_hourly.py` 读 `data/open_plan_tang.json`（weekly800 每次出表自动重写，保留 opened 状态）；7-22 点、每波≤4 条、条间随机 20-50s；`data/open_plan_STOP` 停；token 过期自停。对应 automation FREQ=HOURLY —— **rrule 的 BYHOUR 只能单值**，只能整点跑 + 脚本内判窗口。

## token
- JWT HS512 能读能验、签不出续不了；payload 只有 `uid`+`exp`。微信登录不能脚本化（一次性 code）→ 上限是 mitmproxy 捞 token 写回配置。
- **唯一续期入口** `POST /user/login/wx_login_silence`，body `{code, scene:1005, appid:wx059cd327295ab444}`，要 wx.login 一次性 code → 脚本复刻不了；响应头无 set-token。
- **失效判据**：任何接口返回 `{"code":403,"msg":"登录凭证不合法或已过期"}`（严格校验、过期 3 秒就 403）→ 脚本自停/告警。
- 有效期实测（2026-11-20 抓包）：签发后**整 7 天**；早期"48 小时"未复现，别当硬结论。

## 售后（after_sale_probe.py）
- 列表 POST `/after-sales-biz/as_order_search/v2/search`。ghViewType 10=别人向我发起（要这个）/20=我向上游，互斥；asStatusList=[10,20] 待处理，[] 会拉到 2024 年归档；searchContent 只认手机号/订单号，售后单号用 asOrderNo。
- 「复制信息」无后端（零请求）；asContent 大量为空必须容错；价格单位分。

## 合集通用（act_collection）
- 新建 `act_collection_create`（**body 带 actCollectionId 也照样新建**）；原地改只能 `act_collection_modify`（保住已分享链接）；`actIdList: []` 被忽略；无删除接口。上限 200 条。
- 选团列表复用 `query_ghome_feed_back_v2`（无限流，按 createTime 边界停，置顶+时间倒序）。
- per-act「近 7 天有单」无接口；`get_last_order_by_act_id?actId=` 只给累计第 N 单。详见 2026-11-05.md（fetch_feed_7d.py + collection_fill.py）。
- **自营 vs 帮卖唯一判据**：`POST /activity-biz/activity/detail/frame/help_sale_root {activityId}` 的 `data.activityRootInfo` —— 有值＝帮卖（ghName 是供货商）、**null＝自己供货**。150 走 help_sale_root、160 走 help_sale_child，传错报 5204。
- **自己开的团查不到「帮卖广场详情」**（query_list_by_act_ids 返空）；只有 `POST /activity-detail/activity/detail/general_goods {activityId}` 能拿 goodsId 列表。
- 主页 feed 的 `actItemDTO.ghId` 恒等于账号自己，不能判「谁开的」；`boughtCount` / `goodsAllOrderNum` 都不是本团销量。

## 商品数据接口（所有合集共用）
- `POST /ghdata-biz/api/ghData/queryPageGhGoodsRank`：`dataType=10` 今天、`20` 昨天（30/40/50 无数据）；`orderField=uv|buyQuantity`；**翻页用 `page`（pageNum 无效）**，每页固定 30。
- 销量榜几页就到底＝当天有单商品全量（实测覆盖 147 单 vs 大盘 149）；访问榜取前 30 页再按比例截。`dateTag` 传什么都一样，不能切日期。**平台没有任意时段查询**。

## 汤团「日间集市」每日合集（daily_collection.py）
- 每天 19:00 建 `汤团日间集市M.D`，80~200 条，automation `d343332a`。
- **同款只留一条**（留优先级高的），任一判据命中即算：goodsId/spuId 交集、标题归一化相似 ≥90%、内部图片重合 ≥5 张。`Deduper` 类，倒排索引。
- 缓存 `CACHE_VER=2` 才带 spuId + 全部图片；升级后首次跑重拉约 2950 个团（40 秒）。旧缓存去重退化成只剩 goodsId。
- dry-run 只写 `data/coll_preview.json`，不动 `coll_selected.json`；同日拦截只在 `--apply` 生效。
- 坑：`pick()` 末尾若再 `sorted(picked)` 会把去重剔除项捞回来。
- **「汤团自己发的跟团链接」= 自营的 150**，每天 5-20 条必须进合集；别再扫全库按名字+时间配（老做法扫 200 页仍 0 条）。主页 `ghType=10` feed 里 150/160 混返，翻几页就够；**每页开头混置顶老团（stickyStatus=210），不能按单条 createTime 判停止**。
- 口径：访问量**前 10%**；「当天新开」从 **12:30** 起算；权重 自营必选 110 > 有单 100 > 访问前列 90 > 今日开 50 > 补位 10。

## 汤团² 日间集市（daily_collection2.py）
- 账号 `PsHHJN8_bsMtSbS_b09f0984`**纯帮卖号、无自营团**，automation `099c2a90` 19:00（与主号同点）。
- 它的 **150＝跟上游开的帮卖团**、**160＝自己的子团**，合集放 150；主页要 `ghType=10` 才看得到 150（`ghType=20` 只给 160）。今天约 97 条、48 小时 394 条。
- **价格**在 `frame/help_sale_root` 的 `data.activity.minPrice`，**单位分**；`general_goods` 的 groupBuyPrice 恒 0，不能判 9.9。
- 帮卖品类几乎全是服饰/日用，**食品只有 1~2 条**——「食品不进」在这号上基本不生效。
- 口径：今天开的帮卖全进（剔食品 + minPrice≤990）→ 访问榜前 **35%** 补位到 200 → `dc.Deduper`（留销量高）→ 80~200 条。
- **排序＝今天有单 → 销量 → 访问量 → 创建时间**；**封面 6 款必须主图有模特真人**。
- 真人判定：`tools/facedet.swift`（macOS Vision，首次自动 swiftc 编译，二进制 gitignore）。**脸高占比 ≥0.075 算半身/大头照**（全身约 0.05、纯图 0）；从前 24 个候选挑 6 个。
- 图片 CDN `https://res0.shangshi360.com/` 支持 `?x-oss-process=image/resize,w_300` 缩略；串行 24 张要 4.5 分钟，8 并发 6 秒。
- 缓存 `coll2_goods_cache.json` / `coll2_prices.json` / 留底 `coll2_selected.json`。首条成品：合集 **2158483**「汤团日间集市11.21」132 条（帮卖 93 + 访问前列 39，去重剔 2）。

## 晨间 / 午间 合集（slot_collection.py）
- 汤团、汤团2 各两条：**06:00「M.Dxx主推榜」**（窗口 昨天06:00~今天06:00）、**12:30「M.Dxx午间好品不错过」**（窗口 今天06:00~12:30）。上限 200，口径同日间集市。
- 入口：`slot_collection.py --account tang|tang2 --slot morning|noon [--apply]`；留底 `data/slot_<acc>_<slot>.json`。
- 晨间/午间都是**近似**（多算 00:00~06:00 那段），汇报时必须说明。访问量**按每个榜分别截比例再合并**（直接拼起来截会让今天 0-6 点占满名额）。
- 汤团：主页 ghType=20 翻 40 页（≈30 天、2000 团，2.5 分钟）；自营跟团链接全进；封面 6 款食品。汤团2：ghType=10 只看 150、往回 48 小时，本时段开的全进（剔食品/9.9）；封面 6 款真人。
- automation：`ae0cce44` 06:00 / `d7a45665` 06:10 / `7f28aef9` 12:30 / `c77872a2` 12:40。

## 抓包反推
`.chlz` = zip（meta/req/res 三个 json，body 已明文）→ 用用户级 skill reverse-engineer-app-capture。

## Git / 公开仓库
- origin `git@github.com:Harrydmyh/qjl.git`，**PUBLIC，推=公开**。`data/` 被 .gitignore 排除（必须留）。
- **铁律：memory 绝不写密钥真值**（只写「见 data/config.json」）；示例值只用假值 `PsXXXXXX_bsXXXXXX_xxxxxxxx`（曾 filter-branch 抹除）。
- 推送用 `~/.ssh/id_ed25519`（Harrydmyh）；Harrydm5 只有 pull。数据文件（activities.json、open_log.json、config.json）先备份。

## 前端 web/（Vite+React+TS）
- 规则改 `src/lib/rules.ts`；`npm run dev|build|smoke`；Vercel Root=web。
- agent 环境 npm install 卡死 → 加 `env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY`。
- jsdom 不支持 ESM，冒烟用 esbuild 打 IIFE；fetch 加头别写 `{...init.headers}`。

## index.html 手机端
- 窄屏表格→卡片；表头全选/排序入口消失 → 已加 .list-bar 选择条（#mSelectAll/#sortSel）。供应商排序必须 `localeCompare(b,'zh-Hans-CN')`。
