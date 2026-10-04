import { useEffect, useMemo, useState } from 'react';
import ActivityTable from './ActivityTable';
import { postJson } from '../lib/api';
import { isAlreadyFollowed, isDefaultOpenPick, isOpenBlocked } from '../lib/rules';
import type { Activity, Category, JobSnapshot } from '../types';
import type { LogLine, Ui } from '../lib/ui';

interface Props {
  activities: Activity[];
  ui: Ui;
  lines: LogLine[];
  job: JobSnapshot;
  refreshJob: (reset?: boolean) => Promise<JobSnapshot | undefined>;
  attachJob: () => void;
  jobActive: boolean;
  reloadActivities: () => Promise<void>;
}

type StateFilter = 'all' | 'openable' | 'blocked';

export default function OpenView({
  activities,
  ui,
  lines,
  job,
  refreshJob,
  attachJob,
  jobActive,
  reloadActivities,
}: Props) {
  const [search, setSearch] = useState('');
  const [catFilter, setCatFilter] = useState<'all' | Category>('all');
  const [stateFilter, setStateFilter] = useState<StateFilter>('all');
  const [ghFilter, setGhFilter] = useState('all');
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [delay, setDelay] = useState(3);
  const [jitter, setJitter] = useState(1);
  const [startAt, setStartAt] = useState('');
  const [dryRun, setDryRun] = useState(false);
  const [busy, setBusy] = useState(false);

  const ghs = useMemo(() => {
    const set = new Set(activities.map((a) => a.ghName).filter(Boolean));
    return Array.from(set).sort();
  }, [activities]);

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase();
    return activities
      .filter((a) => {
        if (catFilter !== 'all' && a.category !== catFilter) return false;
        if (ghFilter !== 'all' && a.ghName !== ghFilter) return false;
        if (stateFilter === 'openable' && isOpenBlocked(a)) return false;
        if (stateFilter === 'blocked' && !isOpenBlocked(a)) return false;
        if (q) {
          const blob = `${a.actName || ''} ${a.goodsName || ''} ${a.ghName || ''}`.toLowerCase();
          if (!blob.includes(q)) return false;
        }
        return true;
      })
      .sort((a, b) => (b.startTime || 0) - (a.startTime || 0));
  }, [activities, search, catFilter, ghFilter, stateFilter]);

  // 勾选跟着当前筛选走：默认勾「能开 且 不是已跟过」的
  const visKey = useMemo(() => visible.map((a) => a.actId).join(','), [visible]);
  useEffect(() => {
    setSelected(new Set(visible.filter(isDefaultOpenPick).map((a) => a.actId)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visKey]);

  useEffect(() => {
    attachJob();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const blockedCount = visible.filter(isOpenBlocked).length;
  const followedUnchecked = visible.filter((a) => !isOpenBlocked(a) && isAlreadyFollowed(a)).length;

  const toggle = (actId: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(actId)) next.delete(actId);
      else next.add(actId);
      return next;
    });
  };

  const markOpened = async (actId: number) => {
    const a = activities.find((x) => x.actId === actId);
    const ok = await ui.confirm(
      `把这条记成「我已经开过」？\n\n${a?.goodsName || a?.actName || actId}\n\n之后 5 天内，同款（含改名和别家供应商的版本）都不会再被开。`,
      '标记已开',
    );
    if (!ok) return;
    try {
      const r = await postJson<{ marked: number[]; weak: number[] }>('/api/log/mark', { actIds: [actId] });
      await reloadActivities();
      if (r.weak?.length) {
        await ui.alert('已记入开团记录。⚠️ 这条拿不到商品信息，只能按活动 ID 命中冷却。', '标记已开');
      }
    } catch (e: any) {
      ui.alert('标记失败：' + e.message, '出错了');
    }
  };

  const startOpen = async () => {
    const ids = Array.from(selected);
    if (!ids.length) {
      const reasons: string[] = [];
      if (blockedCount) reasons.push(`${blockedCount} 个不能开（不可开团或同款冷却中）`);
      if (followedUnchecked) reasons.push(`${followedUnchecked} 个是「已跟过」，默认没勾选`);
      await ui.alert(`没有勾选任何活动。\n\n${reasons.join('\n') || '请先勾选。'}`, '没法开始');
      return;
    }
    const ok = await ui.confirm(
      `将开 ${ids.length} 个团。\n\n间隔 ${delay}s${jitter ? ` ± ${jitter}s` : ''}${startAt ? `\n定时开始：${startAt}` : ''}${
        dryRun ? '\n\n（演练模式：只拉模板，不会真的开团）' : ''
      }\n\n任务跑在服务端，关掉这个页面也会继续。`,
      '确认开团',
    );
    if (!ok) return;
    setBusy(true);
    try {
      await postJson('/api/open/start', {
        act_ids: ids,
        dry_run: dryRun,
        delay: Number(delay) || 0,
        jitter: Number(jitter) || 0,
        start_at: startAt || null,
      });
      await refreshJob(true);
    } catch (e: any) {
      ui.alert('开团启动失败：' + e.message, '出错了');
    } finally {
      setBusy(false);
    }
  };

  const cancelOpen = async () => {
    if (!(await ui.confirm('确定要取消当前开团任务？', '取消任务'))) return;
    try {
      await postJson('/api/open/cancel', {});
      await refreshJob();
    } catch (e: any) {
      ui.alert('取消失败：' + e.message, '出错了');
    }
  };

  const pct = job.total ? Math.min((job.index / job.total) * 100, 100) : 0;

  return (
    <>
      <div className="card">
        <div className="row">
          <input
            type="text"
            placeholder="搜活动名 / 商品名 / 团长"
            style={{ width: 220 }}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <select value={catFilter} onChange={(e) => setCatFilter(e.target.value as any)}>
            <option value="all">全部分类</option>
            <option value="new">新活动</option>
            <option value="followed">已跟过</option>
            <option value="reopen">可复开</option>
          </select>
          <select value={stateFilter} onChange={(e) => setStateFilter(e.target.value as StateFilter)}>
            <option value="all">全部状态</option>
            <option value="openable">可开团</option>
            <option value="blocked">不可开（冷却/未开跟团）</option>
          </select>
          <select value={ghFilter} onChange={(e) => setGhFilter(e.target.value)} style={{ maxWidth: 200 }}>
            <option value="all">全部团长（{ghs.length}）</option>
            {ghs.map((g) => (
              <option key={g} value={g}>
                {g}
              </option>
            ))}
          </select>
          <button
            className="btn btn-ghost"
            onClick={() => {
              setSearch('');
              setCatFilter('all');
              setStateFilter('all');
              setGhFilter('all');
            }}
          >
            清除筛选
          </button>
          <span className="stat" style={{ marginLeft: 'auto' }}>
            筛选后 <strong>{visible.length}</strong> / 共 {activities.length} 条 · 已选{' '}
            <strong>{selected.size}</strong>
          </span>
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          <button className="btn btn-ghost btn-sm" onClick={() => setSelected(new Set(visible.filter(isDefaultOpenPick).map((a) => a.actId)))}>
            按默认勾选
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => setSelected(new Set(visible.filter((a) => !isOpenBlocked(a)).map((a) => a.actId)))}>
            勾上全部可开
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => setSelected(new Set())}>
            清空勾选
          </button>
          {blockedCount > 0 && (
            <span className="muted">已跳过 {blockedCount} 个（不可开团或同款冷却中）</span>
          )}
          {followedUnchecked > 0 && (
            <span style={{ color: 'var(--accent)' }}>
              已跟过 {followedUnchecked} 条默认未勾选（确实要再开请手动勾上）
            </span>
          )}
        </div>
      </div>

      <div className="card">
        <h3>开团参数</h3>
        <div className="row">
          <div className="field">
            <label>间隔（秒）</label>
            <input type="number" style={{ width: 80 }} value={delay} onChange={(e) => setDelay(Number(e.target.value))} />
          </div>
          <div className="field">
            <label>随机浮动 ±（秒）</label>
            <input type="number" style={{ width: 90 }} value={jitter} onChange={(e) => setJitter(Number(e.target.value))} />
          </div>
          <div className="field">
            <label>定时开始（留空 = 立即）</label>
            <input type="datetime-local" value={startAt} onChange={(e) => setStartAt(e.target.value)} />
          </div>
          <label className="check" style={{ marginTop: 16 }}>
            <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} /> 演练（不真开）
          </label>
          <div className="row" style={{ marginLeft: 'auto', marginTop: 16 }}>
            <button className="btn btn-primary" disabled={busy || jobActive} onClick={startOpen}>
              开始开团（{selected.size}）
            </button>
            {jobActive && (
              <button className="btn btn-danger" onClick={cancelOpen}>
                取消任务
              </button>
            )}
          </div>
        </div>

        {job.state !== 'idle' && (
          <div style={{ marginTop: 12 }}>
            <div className="progress">
              <i style={{ width: `${pct}%` }} />
            </div>
            <div className="stat">
              状态 <strong>{job.state}</strong>
              {job.state === 'scheduled' && job.remaining != null ? ` · ${job.remaining}s 后开始` : ''}
              {job.total ? ` · 进度 ${job.index}/${job.total}` : ''}
              {` · 成功 ${job.ok} · 失败 ${job.fail}`}
              {job.current ? <div className="muted">当前：{job.current}</div> : null}
              {job.error ? <div style={{ color: 'var(--red)' }}>错误：{job.error}</div> : null}
            </div>
          </div>
        )}
      </div>

      <div className="card" style={{ padding: 0 }}>
        <ActivityTable
          items={visible}
          selectable
          selected={selected}
          onToggle={toggle}
          onToggleAll={(checked) =>
            setSelected(checked ? new Set(visible.filter((a) => !isOpenBlocked(a)).map((a) => a.actId)) : new Set())
          }
          onMarkOpened={markOpened}
        />
      </div>

      <div className="card">
        <h3>任务日志（服务端在跑，关掉页面也会继续）</h3>
        <div className="log">
          {lines.length === 0 ? (
            <span className="muted">还没有任务日志。</span>
          ) : (
            lines.map((l, i) => (
              <div key={i} className={l.kind}>
                {l.text}
              </div>
            ))
          )}
        </div>
      </div>
    </>
  );
}
