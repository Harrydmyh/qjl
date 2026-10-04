// 与后端 core.parse_item 一一对应。后端加字段时这里也要加。

export type Category = 'new' | 'followed' | 'reopen';

export interface Activity {
  actId: number;
  actName: string;
  ghName: string;
  starGh?: boolean;
  price: number;
  /** 供货佣金 */
  earning: number;
  /** 跟团佣金 */
  followEarning: number;
  /** followEarning 非 null = 已开跟团功能，能开团 */
  hasFollowRelation?: boolean;
  activityType?: number;
  commissionPct?: string;
  totalOrders?: number;
  helpSaleCount?: number;
  goodsName?: string;
  goodsIds?: number[];
  goodsNames?: string[];
  skuCount?: number;
  hadHelpSale?: boolean;
  isFollow?: boolean;
  /** isFollow 的布尔形式：平台记录里你已经开过这个团 */
  alreadyFollowed?: boolean;
  startTime?: number;
  startTimeStr?: string;
  upStreamType?: number;
  category: Category;
  bestActId?: number | null;
  bestOrders?: number | null;
  daysSinceOpen?: number | null;
  recentlyOpened?: boolean;
  /** 同款被合并掉的其它来源 */
  altSources?: { ghName: string; category: Category }[];
}

export interface LogEntry {
  time: string;
  actId: number;
  newActId?: number | null;
  actName?: string;
  goodsIds?: number[];
  goodsNames?: string[];
  status: string;
  error?: string;
  /** 不是本工具开的团，是用户手动「标记已开」补录的 */
  manual?: boolean;
}

export interface TokenInfo {
  uid?: number;
  exp_str?: string;
  remaining_seconds?: number;
  valid?: boolean;
  error?: string;
}

export interface ServerConfig {
  gh_id?: string;
  uid?: string;
}

export interface ConfigResponse {
  config: ServerConfig;
  token_preview: string;
  token_info: TokenInfo;
  access_enabled: boolean;
  categories: Record<string, string>;
}

/** /api/fetch 流式事件 */
export type FetchEvent =
  | { type: 'page'; page: string; total: number }
  | { type: 'progress'; page: string; added: number; total: number; grand_total?: number }
  | { type: 'error'; msg: string }
  | {
      type: 'done';
      total_raw: number;
      dropped: number;
      new_raw: number;
      reopen_raw: number;
      followed_raw: number;
      deduped: number;
      new_count: number;
      reopen_count: number;
      followed_count: number;
      cooldown_count: number;
      cooldown_days: number;
      total_available: number;
      items: Activity[];
    };

export type JobState = 'idle' | 'scheduled' | 'running' | 'done' | 'cancelled' | 'failed';

export interface JobEvent {
  t: string;
  kind: 'info' | 'ok' | 'err' | string;
  msg: string;
}

/** /api/open/status 的快照 */
export interface JobSnapshot {
  id: number;
  state: JobState;
  start_ts: number | null;
  remaining: number | null;
  total: number;
  index: number;
  ok: number;
  fail: number;
  current: string | null;
  next_wait: number | null;
  dry_run: boolean;
  delay: number;
  jitter: number;
  error: string | null;
  created_at: string | null;
  finished_at: string | null;
  event_count?: number;
  events?: JobEvent[];
}
