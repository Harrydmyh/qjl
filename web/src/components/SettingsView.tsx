import { useEffect, useState } from 'react';
import { apiJson, getAccessKey, getApiBase, isSameOrigin, postJson, setAccessKey, setApiBase } from '../lib/api';
import type { ConfigResponse } from '../types';
import type { Ui } from '../lib/ui';

export default function SettingsView({ ui, onSaved }: { ui: Ui; onSaved: () => void }) {
  const [base, setBase] = useState(getApiBase());
  const [key, setKey] = useState(getAccessKey());
  const [token, setToken] = useState('');
  const [ghId, setGhId] = useState('');
  const [uid, setUid] = useState('');
  const [info, setInfo] = useState<ConfigResponse | null>(null);
  const [err, setErr] = useState('');
  const [testing, setTesting] = useState(false);

  const loadServerCfg = async () => {
    setErr('');
    try {
      const r = await apiJson<ConfigResponse>('/api/config');
      setInfo(r);
      setGhId(r.config?.gh_id || '');
      setUid(String(r.config?.uid || ''));
    } catch (e: any) {
      setErr(e.message);
      setInfo(null);
    }
  };

  useEffect(() => {
    loadServerCfg();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const saveLocal = async () => {
    setApiBase(base);
    setAccessKey(key);
    setTesting(true);
    try {
      await apiJson('/api/access');
      await loadServerCfg();
      await ui.alert('连接成功，配置已保存到本机浏览器。', '设置');
      onSaved();
    } catch (e: any) {
      await ui.alert(
        '连不上后端：' + e.message + '\n\n检查一下：\n1) 后端地址是否填成了 https://…\n2) 访问密钥对不对\n3) 那台机器上的服务是不是在跑',
        '连接失败',
      );
    } finally {
      setTesting(false);
    }
  };

  const saveToken = async () => {
    try {
      await postJson('/api/config', { auth_token: token, gh_id: ghId, uid });
      setToken('');
      await loadServerCfg();
      await ui.alert('服务端凭证已保存。', '设置');
    } catch (e: any) {
      await ui.alert('保存失败：' + e.message, '出错了');
    }
  };

  const ti = info?.token_info;

  return (
    <>
      <div className="card">
        <h3>后端连接</h3>
        <div className="muted" style={{ marginBottom: 10, lineHeight: 1.7 }}>
          当前模式：{isSameOrigin() ? '同源（页面和后端在一起，什么都不用填）' : '跨域（页面挂在静态托管上，请求打回你的机器）'}
          <br />
          也可以用 <code>?api=https://后端地址&amp;k=访问密钥</code> 打开一次，会自动记住。
        </div>
        <div className="field" style={{ marginBottom: 10 }}>
          <label>后端地址（本机同源就留空；挂公网填 https://…）</label>
          <input
            type="text"
            placeholder="https://你的mac.xxxxx.ts.net"
            value={base}
            onChange={(e) => setBase(e.target.value)}
          />
        </div>
        <div className="field" style={{ marginBottom: 12 }}>
          <label>访问密钥（后端 config.json 里的 access_key）</label>
          <input type="text" placeholder="留空 = 后端没启用密钥" value={key} onChange={(e) => setKey(e.target.value)} />
        </div>
        <div className="row">
          <button className="btn btn-primary" disabled={testing} onClick={saveLocal}>
            {testing ? '测试中…' : '保存并测试连接'}
          </button>
          <button className="btn btn-ghost" onClick={loadServerCfg}>
            重新读取后端状态
          </button>
        </div>
        {err && <div className="banner err" style={{ marginTop: 12, borderRadius: 6 }}>{err}</div>}
      </div>

      <div className="card">
        <h3>服务端凭证（保存在后端所在的那台机器上）</h3>
        {info ? (
          <div className="stat" style={{ lineHeight: 1.9 }}>
            当前 token：<code>{info.token_preview || '（空）'}</code>
            <br />
            有效期：{ti?.valid ? <b style={{ color: 'var(--green)' }}>{ti.exp_str}</b> : <b style={{ color: 'var(--red)' }}>已失效</b>}
            {ti?.remaining_seconds ? ` （还剩约 ${Math.floor(ti.remaining_seconds / 3600)} 小时）` : ''}
            <br />
            访问密钥：{info.access_enabled ? '已启用' : '未启用（任何人都能访问）'}
            <br />
            <span className="muted">换 token 的流程：群接龙里抓一次新 token → 填到下面 → 保存。</span>
          </div>
        ) : (
          <div className="muted">还读不到后端状态（先在上面把连接配好）。</div>
        )}
        <div className="row" style={{ marginTop: 12 }}>
          <div className="field" style={{ flex: 1, minWidth: 280 }}>
            <label>新的 auth_token（留空 = 不修改）</label>
            <input type="text" placeholder="eyJhbGciOiJIUzUxMiJ9..." value={token} onChange={(e) => setToken(e.target.value)} />
          </div>
          <div className="field">
            <label>gh_id</label>
            <input type="text" style={{ width: 220 }} value={ghId} onChange={(e) => setGhId(e.target.value)} />
          </div>
          <div className="field">
            <label>uid</label>
            <input type="text" style={{ width: 130 }} value={uid} onChange={(e) => setUid(e.target.value)} />
          </div>
          <button className="btn btn-primary" style={{ marginTop: 16 }} onClick={saveToken}>
            保存
          </button>
        </div>
      </div>
    </>
  );
}
