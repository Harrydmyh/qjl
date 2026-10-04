import { useCallback, useEffect, useRef, useState } from 'react';
import { apiJson } from '../lib/api';
import type { JobSnapshot } from '../types';

const IDLE: JobSnapshot = {
  id: 0,
  state: 'idle',
  start_ts: null,
  remaining: null,
  total: 0,
  index: 0,
  ok: 0,
  fail: 0,
  current: null,
  next_wait: null,
  dry_run: false,
  delay: 0,
  jitter: 0,
  error: null,
  created_at: null,
  finished_at: null,
  event_count: 0,
};

/**
 * 开团任务状态。
 *
 * 任务跑在**服务端**（后端进程里），所以关掉页面不影响它继续跑；
 * 页面重开时用 `since=0` 把历史事件全部重放回来。
 */
export function useJob(log: (line: string, kind?: string) => void) {
  const [job, setJob] = useState<JobSnapshot>(IDLE);
  const [events, setEvents] = useState<{ t: string; kind: string; msg: string }[]>([]);
  const eventCount = useRef(0);
  const jobIdRef = useRef(0);
  const timer = useRef<number | null>(null);

  const refresh = useCallback(
    async (reset = false) => {
      const since = reset ? 0 : eventCount.current;
      const snap = await apiJson<JobSnapshot>(`/api/open/status?since=${since}`);
      eventCount.current = snap.event_count ?? 0;
      const incoming = snap.events || [];
      if (reset || snap.id !== jobIdRef.current) {
        jobIdRef.current = snap.id;
        setEvents(incoming);
      } else {
        setEvents((prev) => [...prev, ...incoming]);
      }
      incoming.forEach((e) => log(`[${e.t}] ${e.msg}`, e.kind));
      setJob(snap);
      return snap;
    },
    [log],
  );

  // 跑着的时候每秒拉一次；空闲时停掉，别白耗
  useEffect(() => {
    const active = job.state === 'running' || job.state === 'scheduled';
    if (timer.current) {
      window.clearInterval(timer.current);
      timer.current = null;
    }
    if (!active) return;
    timer.current = window.setInterval(() => {
      refresh().catch(() => {});
    }, 1000);
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [job.state, refresh]);

  /** 页面重新打开 / 切到开团页时接回进度 */
  const attach = useCallback(() => {
    refresh(true).catch(() => {});
  }, [refresh]);

  return { job, events, refresh, attach, active: job.state === 'running' || job.state === 'scheduled' };
}
