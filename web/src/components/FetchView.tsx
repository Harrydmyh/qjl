import { useRef, useState } from 'react';
import { streamEvents } from '../lib/api';
import type { Activity, FetchEvent } from '../types';
import type { LogLine, Ui } from '../lib/ui';

interface Props {
  ui: Ui;
  onDone: (items: Activity[]) => void;
  log: (line: string, kind?: string) => void;
  lines: LogLine[];
}

export default function FetchView({ ui, onDone, lines }: Props) {
  const [mode, setMode] = useState<'days' | 'dates'>('days');
  const [days, setDays] = useState(7);
  const [dates, setDates] = useState('');
  const [includeFollowed, setIncludeFollowed] = useState(true);
  const [keywords, setKeywords] = useState('');
  const [running, setRunning] = useState(false);
  const [percent, setPercent] = useState(0);
  const [summary, setSummary] = useState<FetchEvent & { type: 'done' } | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const start = async () => {
    setRunning(true);
    setPercent(0);
    setSummary(null);
    const ac = new AbortController();
    abortRef.current = ac;
    let total = 0;
    try {
      await streamEvents(
        '/api/fetch',
        {
          days_range: Number(days) || 7,
          dates:
            mode === 'dates'
              ? dates
                  .split(/[,\s]+/)
                  .map((s) => s.trim())
                  .filter(Boolean)
              : [],
          include_followed: includeFollowed,
          keywords: keywords
            .split(/[,\s，]+/)
            .map((s) => s.trim())
            .filter(Boolean),
        },
        (ev: FetchEvent) => {
          if (ev.type === 'page') {
            total = ev.total || total;
          } else if (ev.type === 'progress') {
            const t = ev.grand_total || ev.total || 0;
            if (t) setPercent(Math.min((ev.total / t) * 100, 99));
          } else if (ev.type === 'done') {
            setPercent(100);
            setSummary(ev);
            onDone(ev.items || []);
          }
        },
        ac.signal,
      );
    } catch (e: any) {
      if (e?.name !== 'AbortError') ui.alert('拉取失败：' + e.message, '出错了');
    } finally {
      setRunning(false);
      abortRef.current = null;
    }
  };

  return (
    <>
      <div className="card">
        <h3>拉取范围</h3>
        <div className="row" style={{ marginBottom: 10 }}>
          <label className="check">
            <input type="radio" checked={mode === 'days'} onChange={() => setMode('days')} /> 最近
          </label>
          <input
            type="number"
            style={{ width: 64 }}
            value={days}
            disabled={mode !== 'days'}
            onChange={(e) => setDays(Number(e.target.value))}
          />
          <span className="muted">天</span>
          <label className="check" style={{ marginLeft: 14 }}>
            <input type="radio" checked={mode === 'dates'} onChange={() => setMode('dates')} /> 指定日期
          </label>
          <input
            type="text"
            style={{ width: 260 }}
            placeholder="2026-09-20, 2026-09-21"
            value={dates}
            disabled={mode !== 'dates'}
            onChange={(e) => setDates(e.target.value)}
          />
        </div>
        <div className="row">
          <div className="field" style={{ flex: 1, minWidth: 260 }}>
            <label>定向供应商 / 关键词（可选，多个用逗号隔开；服务端搜索，比搜商品名更准）</label>
            <input
              type="text"
              placeholder="狂奔的小绵羊, 益能云天"
              value={keywords}
              onChange={(e) => setKeywords(e.target.value)}
            />
          </div>
          <label className="check" style={{ marginTop: 16 }}>
            <input
              type="checkbox"
              checked={includeFollowed}
              onChange={(e) => setIncludeFollowed(e.target.checked)}
            />
            含「已跟过」的活动（复团候选，默认保留）
          </label>
        </div>
        <div className="row" style={{ marginTop: 14 }}>
          <button className="btn btn-primary" disabled={running} onClick={start}>
            {running ? '拉取中…' : '开始拉取'}
          </button>
          {running && (
            <button
              className="btn btn-danger"
              onClick={() => {
                abortRef.current?.abort();
                setRunning(false);
              }}
            >
              中断
            </button>
          )}
        </div>
        <div className="progress">
          <i style={{ width: `${percent}%` }} />
        </div>
        {summary && (
          <div className="stat">
            原始 <strong>{summary.total_raw}</strong> 条 · 去重合并 <strong>{summary.deduped}</strong> 条 · 丢弃{' '}
            <strong>{summary.dropped}</strong> 条 → 可用 <strong>{summary.total_available}</strong> 条
            <div className="muted" style={{ marginTop: 4 }}>
              分桶：新活动 {summary.new_count} · 已跟过 {summary.followed_count} · 可复开 {summary.reopen_count} ·
              同款冷却中 {summary.cooldown_count}（{summary.cooldown_days} 天窗口）
            </div>
          </div>
        )}
      </div>

      <div className="card">
        <h3>实时日志</h3>
        <div className="log">
          {lines.length === 0 ? (
            <span className="muted">还没有日志。</span>
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
