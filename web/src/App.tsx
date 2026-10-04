import { useCallback, useEffect, useMemo, useState } from 'react';
import Sidebar from './components/Sidebar';
import Dialog, { type DialogState } from './components/Dialog';
import FetchView from './components/FetchView';
import OpenView from './components/OpenView';
import LogView from './components/LogView';
import SettingsView from './components/SettingsView';
import { apiJson, getApiBase, isSameOrigin, onUnauthorized } from './lib/api';
import { isAlreadyFollowed, isOpenBlocked, isRecentlyOpened } from './lib/rules';
import { useJob } from './hooks/useJob';
import type { Activity } from './types';
import type { LogLine, Ui, View } from './lib/ui';

export default function App() {
  const [view, setView] = useState<View>('fetch');
  const [activities, setActivities] = useState<Activity[]>([]);
  const [logCount, setLogCount] = useState(0);
  const [fetchLines, setFetchLines] = useState<LogLine[]>([]);
  const [jobLines, setJobLines] = useState<LogLine[]>([]);
  const [dlg, setDlg] = useState<DialogState | null>(null);
  const [needKey, setNeedKey] = useState(false);
  const [connErr, setConnErr] = useState('');

  const alert = useCallback(
    (msg: string, title = '提示') =>
      new Promise<void>((resolve) => setDlg({ title, msg, withCancel: false, resolve: () => resolve() })),
    [],
  );
  const confirm = useCallback(
    (msg: string, title = '确认') =>
      new Promise<boolean>((resolve) => setDlg({ title, msg, withCancel: true, resolve })),
    [],
  );
  const ui: Ui = useMemo(() => ({ alert, confirm }), [alert, confirm]);

  const pushFetch = useCallback((text: string, kind = 'info') => {
    setFetchLines((p) => [...p, { text, kind }].slice(-600));
  }, []);
  const pushJob = useCallback((text: string, kind = 'info') => {
    setJobLines((p) => [...p, { text, kind }].slice(-600));
  }, []);

  const reloadActivities = useCallback(async () => {
    const r = await apiJson<{ items: Activity[] }>('/api/activities');
    setActivities(r.items || []);
    setConnErr('');
  }, []);

  const reloadLogCount = useCallback(async () => {
    try {
      const r = await apiJson<{ items: unknown[] }>('/api/log');
      setLogCount((r.items || []).length);
    } catch {
      // 记录条数拿不到不影响主流程
    }
  }, []);

  useEffect(() => {
    onUnauthorized(() => setNeedKey(true));
    return () => onUnauthorized(null);
  }, []);

  useEffect(() => {
    (async () => {
      try {
        await reloadActivities();
        await reloadLogCount();
      } catch (e: any) {
        setConnErr(e.message || String(e));
      }
    })();
  }, [reloadActivities, reloadLogCount]);

  const { job, attach, refresh, active } = useJob(pushJob);

  const counts = useMemo(
    () => ({
      openable: activities.filter((a) => !isOpenBlocked(a)).length,
      followed: activities.filter((a) => isAlreadyFollowed(a)).length,
      cooldown: activities.filter((a) => isRecentlyOpened(a)).length,
    }),
    [activities],
  );

  const titles: Record<View, string> = {
    fetch: '刷新列表',
    open: '活动 & 开团',
    log: '开团记录',
    settings: '设置',
  };

  return (
    <div className="app">
      <Sidebar
        view={view}
        onChange={setView}
        total={activities.length}
        openable={counts.openable}
        followed={counts.followed}
        cooldown={counts.cooldown}
        logCount={logCount}
      />
      <div className="main">
        {needKey && (
          <div className="banner err">
            需要访问密钥：请在网址最后加上 <code>?k=你的密钥</code> 再打开本页（带一次就会记住）。
            <button className="btn btn-ghost btn-sm" style={{ marginLeft: 10 }} onClick={() => setNeedKey(false)}>
              知道了
            </button>
          </div>
        )}
        {connErr && !needKey && (
          <div className="banner err">
            连不上后端：{connErr}
            {isSameOrigin() && (
              <>
                <br />
                当前是「同源」模式，说明页面和后端不在一起。去「设置」里填后端地址，或者用{' '}
                <code>?api=https://后端地址&amp;k=密钥</code> 打开。
              </>
            )}
          </div>
        )}

        <div className="topbar">
          <span className="title">{titles[view]}</span>
          <div className="right">
            <span className="muted">
              {isSameOrigin() ? '同源' : getApiBase()} · {activities.length} 条
            </span>
            <button
              className="btn btn-ghost btn-sm"
              onClick={async () => {
                try {
                  await reloadActivities();
                  await reloadLogCount();
                } catch (e: any) {
                  setConnErr(e.message);
                }
              }}
            >
              重新载入
            </button>
          </div>
        </div>

        <div className="content">
          {view === 'fetch' && (
            <FetchView
              ui={ui}
              lines={fetchLines}
              log={pushFetch}
              onDone={(items) => {
                setActivities(items);
                reloadLogCount();
              }}
            />
          )}
          {view === 'open' && (
            <OpenView
              activities={activities}
              ui={ui}
              lines={jobLines}
              job={job}
              refreshJob={refresh}
              attachJob={attach}
              jobActive={active}
              reloadActivities={reloadActivities}
            />
          )}
          {view === 'log' && <LogView ui={ui} />}
          {view === 'settings' && (
            <SettingsView
              ui={ui}
              onSaved={async () => {
                setNeedKey(false);
                try {
                  await reloadActivities();
                  await reloadLogCount();
                } catch (e: any) {
                  setConnErr(e.message);
                }
              }}
            />
          )}
        </div>
      </div>
      <Dialog
        state={dlg}
        onClose={(v) => {
          const d = dlg;
          setDlg(null);
          d?.resolve(v);
        }}
      />
    </div>
  );
}
