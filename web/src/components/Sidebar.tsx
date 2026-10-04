import type { View } from '../lib/ui';

interface Props {
  view: View;
  onChange: (v: View) => void;
  total: number;
  openable: number;
  followed: number;
  cooldown: number;
  logCount: number;
}

const ITEMS: { key: View; label: string }[] = [
  { key: 'fetch', label: '刷新列表' },
  { key: 'open', label: '活动 & 开团' },
  { key: 'log', label: '开团记录' },
  { key: 'settings', label: '设置' },
];

export default function Sidebar({ view, onChange, total, openable, followed, cooldown, logCount }: Props) {
  const countOf = (key: View): number | null => {
    if (key === 'open') return total || null;
    if (key === 'log') return logCount || null;
    return null;
  };
  return (
    <aside className="sidebar">
      <div className="brand">
        开团<span>助手</span>
      </div>
      {ITEMS.map((it) => {
        const n = countOf(it.key);
        return (
          <button
            key={it.key}
            className={'nav-item' + (view === it.key ? ' active' : '')}
            onClick={() => onChange(it.key)}
          >
            <span>{it.label}</span>
            {n != null && <span className="nav-count">{n}</span>}
          </button>
        );
      })}
      <div className="spacer" />
      <div className="hint">
        可开 <b>{openable}</b> · 已跟过 <b>{followed}</b>
        <br />
        冷却中 <b>{cooldown}</b>
      </div>
    </aside>
  );
}
