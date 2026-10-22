# 项目长期注意事项（开团助手 / 群接龙）

## 运行
- 启动 `python3 server.py`（系统 Python，3.13 没 fastapi），端口 8765。
- 页面每次从磁盘读 → **改前端不重启，改 core/server 必须重启**。后台起只能 start_new_session=True。
- 停服务别用 `pkill -f server.py`（误杀工具自身）：`lsof -nP -iTCP:8765 -sTCP:LISTEN -t | xargs kill`。
- 用户报 Failed to fetch → 先 lsof，没有=服务没起。常驻/launchd 已全删。
- 当服务器必须插电；合盖必睡 → `sudo pmset -a disablesleep 1`（恢复 0）。

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
- 帮卖两套模式，工具只做「跟团」：跟团 `for_follow/{actId}/{ghId}`+`/act/release`；子团 `for_leader/…`+`help_sale_child/act/release`，activityType **10→5830，150/160/210 可开**。新号 uid155475458 只能走子团，候选来自 `ghome-feed/act_recommend/get_recommend_acts`。
  请求体两坑：activitySettingsParam 用成功样本补齐（缺 showAllGroupOrder 报 5259）；手机号带 `?ghId=` 打 get_user_input → inputMap["210"]。
- open_log 必须同时写 goodsIds+goodsNames；commissionPercent 绝不能为 null（整团佣金清零）；拉取别发 firstCategoryIdList。
- 批量详情 query_list_by_act_ids 整批同生共死（一个无效 actId 整批返 0，已加逐个重试）；keyword 搜款号更准。
- 看不到自己开的团 →「标记已开」/api/log/mark 补录；isFollow→alreadyFollowed 默认不勾选。
- 同款判定三条判据：goodsId 交集；全部规格商品名同款（匹配名占少方≥50%）；多规格价格指纹（Dice≥0.45）。阈值 0.45，调参必跑回归。
- 「同款 5 天内已开」= 不丢弃、可见、不开团；图片相似度暂不可用。

## token
- JWT HS512 能读能验、签不出续不了；无 refresh；**48 小时**到期重抓。微信登录不能脚本化（一次性 code）→ 上限是 mitmproxy 捞 token 写回配置。

## 售后（after_sale_probe.py）
- 列表 POST /after-sales-biz/as_order_search/v2/search。ghViewType 10=别人向我发起（要这个）/20=我向上游，互斥；asStatusList=[10,20] 待处理，[] 会拉到 2024 年归档；searchContent 只认手机号/订单号，售后单号用 asOrderNo。
- 「复制信息」无后端（handleCopyOrderData 零请求）；asContent 大量为空必须容错；价格单位分。

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
