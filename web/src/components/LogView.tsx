import { useEffect, useState } from 'react';
import { apiFetch, apiJson } from '../lib/api';
import type { LogEntry } from '../types';
import type { Ui } from '../lib/ui';

export default function LogView({ ui }: { ui: Ui }) {
  const [items, setItems] = useState<LogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');

  const load = async () => {
    setLoading(true);
    setErr('');
    try {
      const r = await apiJson<{ items: LogEntry[] }>('/api/log');
      setItems(r.items || []);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const clear = async () => {
    if (!(await ui.confirm('清空全部开团记录？\n\n注意：这份记录同时是「同款 5 天冷却」的唯一依据，清掉之后同款可能被重复开。', '清空记录')))
      return;
    await apiFetch('/api/log', { method: 'DELETE' });
    await load();
  };

  return (
    <div className="card" style={{ padding: 0 }}>
      <div className="row" style={{ padding: 14 }}>
        <h3 style={{ margin: 0 }}>开团记录（{items.length}）</h3>
        <span className="muted">同款 5 天冷却是靠这份记录算的</span>
        <div className="row" style={{ marginLeft: 'auto' }}>
          <button className="btn btn-ghost btn-sm" onClick={load}>
            刷新
          </button>
          <button className="btn btn-danger btn-sm" onClick={clear}>
            清空
          </button>
        </div>
      </div>
      {err && <div className="banner err">读取失败：{err}</div>}
      {loading ? (
        <div className="empty">读取中…</div>
      ) : items.length === 0 ? (
        <div className="empty">还没有开团记录</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>结果</th>
              <th>来源 actId</th>
              <th>新团 actId</th>
              <th>商品</th>
            </tr>
          </thead>
          <tbody>
            {items.map((e, i) => (
              <tr key={`${e.actId}-${e.time}-${i}`}>
                <td style={{ whiteSpace: 'nowrap', color: 'var(--text3)', fontSize: 11 }}>{e.time}</td>
                <td>
                  {e.status === 'ok' ? (
                    <span className="tag tag-new">{e.manual ? '手动标记' : '成功'}</span>
                  ) : (
                    <span className="tag tag-noopen">失败</span>
                  )}
                  {e.error ? <div className="name-sub" style={{ color: 'var(--red)' }}>{e.error}</div> : null}
                </td>
                <td style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>{e.actId}</td>
                <td style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>{e.newActId || '—'}</td>
                <td>
                  <div className="name-main" style={{ fontSize: 12 }}>
                    {(e.goodsNames || []).join(' / ') || <span className="muted">（无商品名，只能按 actId 命中冷却）</span>}
                  </div>
                  {e.actName ? <div className="name-sub">{e.actName}</div> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
