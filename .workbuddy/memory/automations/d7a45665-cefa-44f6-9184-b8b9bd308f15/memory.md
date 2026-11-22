# 汤团² 晨间主推榜（slot_collection --account tang2 --slot morning）

命令：`env -u http_proxy ... /opt/homebrew/bin/python3 -u slot_collection.py --account tang2 --slot morning --apply`
固定 06:10 执行，标题 `M.D汤团2主推榜`，同一天同时段脚本自带拦截。

## 执行记录
- 2026-11-22：成功。合集 ID **2160710**，「11.22汤团2主推榜」198 条。
  本时段帮卖 167 条（去低值后 163 必进）＋ 访问前 35% 补位 37 条；食品剔 3、9.9 以下剔 6；去重剔 2（商品同源 1、图片重合 1）；封面候选 24，真人主图 7。
  预览表 `周报/汤团2晨间预览_20261122.xlsx`，状态 `data/slot_tang2_morning.json`。

## 已知坑
- **耗时异常**：正常约 2 分钟；2026-11-22 这次 `拉最低价 69 条` 阶段卡了约 32 分钟（进程 sleep 在 6 条 ESTABLISHED 连接上、CPU 0%），最终自行恢复。
  未改动代码；若再复现，优先怀疑 `fetch_price` 的 20s 超时对挂死连接不生效，可给 session 加更大超时或整体看门狗。
- 卡住时**不要重复启动**，等同一次运行结束或确认进程已死再重跑。
