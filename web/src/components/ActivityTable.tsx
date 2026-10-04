import type { Activity } from '../types';
import { CAT_LABEL, canOpenAct, cooldownText, commissionLabel, commissionOf, isAlreadyFollowed, isRecentlyOpened } from '../lib/rules';

interface Props {
  items: Activity[];
  selectable?: boolean;
  selected?: Set<number>;
  onToggle?: (actId: number) => void;
  onToggleAll?: (checked: boolean) => void;
  onMarkOpened?: (actId: number) => void;
  emptyText?: string;
}

export default function ActivityTable({
  items,
  selectable = false,
  selected,
  onToggle,
  onToggleAll,
  onMarkOpened,
  emptyText = '没有符合条件的活动',
}: Props) {
  if (!items.length) return <div className="empty">{emptyText}</div>;

  const checkedCount = selectable && selected ? items.filter((a) => selected.has(a.actId)).length : 0;
  const allChecked = selectable && items.length > 0 && checkedCount === items.length;

  return (
    <table>
      <thead>
        <tr>
          {selectable && (
            <th style={{ width: 34 }}>
              <input
                type="checkbox"
                checked={allChecked}
                ref={(el) => {
                  if (el) el.indeterminate = checkedCount > 0 && !allChecked;
                }}
                onChange={(e) => onToggleAll?.(e.target.checked)}
              />
            </th>
          )}
          <th>时间</th>
          <th>活动</th>
          <th className="num">价格</th>
          <th className="num">佣金</th>
          <th className="num">单量</th>
          <th>商品</th>
          <th>团长</th>
        </tr>
      </thead>
      <tbody>
        {items.map((a) => {
          const blocked = !canOpenAct(a) || isRecentlyOpened(a);
          return (
            <tr key={a.actId} className={blocked ? 'blocked' : ''}>
              {selectable && (
                <td>
                  <input
                    type="checkbox"
                    disabled={blocked}
                    checked={!!selected?.has(a.actId)}
                    onChange={() => onToggle?.(a.actId)}
                  />
                </td>
              )}
              <td style={{ whiteSpace: 'nowrap', fontSize: 11, color: 'var(--text3)' }}>
                {a.startTimeStr || '—'}
                {a.category === 'reopen' && (
                  <div style={{ color: 'var(--yellow)', marginTop: 2 }}>
                    复开{a.daysSinceOpen != null ? ` · ${a.daysSinceOpen}天前` : ''}
                  </div>
                )}
                {a.category === 'followed' && (
                  <div style={{ marginTop: 2 }}>
                    <span className="tag tag-followed">已跟过</span>
                  </div>
                )}
                {!canOpenAct(a) && (
                  <div style={{ marginTop: 2 }}>
                    <span className="tag tag-noopen" title="未开启跟团功能（followEarning 为空），开团必定失败">
                      不可开团
                    </span>
                  </div>
                )}
                {isRecentlyOpened(a) && (
                  <div style={{ marginTop: 2 }}>
                    <span className="tag tag-cool" title="这款（含同款的其他供应商版本）5 天内已经开过，满 5 天自动恢复">
                      {cooldownText(a)}
                    </span>
                  </div>
                )}
                {isAlreadyFollowed(a) && !isRecentlyOpened(a) && (
                  <div style={{ marginTop: 2 }}>
                    <span className="tag tag-followed" title="平台记录里你已经开过/跟过这个团，开团面板默认不勾选">
                      已跟过
                    </span>
                  </div>
                )}
              </td>
              <td className="name-main" style={{ minWidth: 260 }}>
                {a.actName}
                {a.bestActId ? (
                  <div className="name-sub" style={{ color: 'var(--yellow)' }}>
                    ↑ 同款最优 {a.bestOrders} 单（actId:{a.bestActId}）
                  </div>
                ) : null}
                {a.altSources?.length ? (
                  <div className="name-sub" title="同款来自多个来源，已合并成一行（显示的是数据最好的那条）">
                    同款另有 {a.altSources.length} 个来源：
                    {a.altSources.map((s) => `${s.ghName}·${CAT_LABEL[s.category] || s.category}`).join(' / ')}
                  </div>
                ) : null}
              </td>
              <td className="num">¥{(a.price || 0).toFixed(0)}</td>
              <td className="num earn">
                ¥{commissionOf(a).toFixed(2)}
                <div className="name-sub">{commissionLabel(a)}</div>
              </td>
              <td className="num">{a.totalOrders || 0}</td>
              <td style={{ minWidth: 180 }}>
                <div className="name-main" style={{ fontSize: 12 }}>
                  {a.goodsName}
                </div>
                <div className="name-sub">
                  {a.commissionPct ? `佣金率 ${a.commissionPct}` : ''}
                  {a.skuCount && a.skuCount > 1 ? `${a.commissionPct ? ' · ' : ''}${a.skuCount} 个规格` : ''}
                </div>
              </td>
              <td style={{ whiteSpace: 'nowrap' }}>
                {a.ghName}
                {a.starGh ? <span className="tag tag-star"> 星</span> : null}
                {onMarkOpened && (
                  <div style={{ marginTop: 4 }}>
                    <button
                      className="btn btn-ghost btn-sm"
                      title="在工具外（比如直接在群接龙里）已经开过这款？点一下记进开团记录，之后 5 天内同款都不会再被开"
                      onClick={() => onMarkOpened(a.actId)}
                    >
                      标记已开
                    </button>
                  </div>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
