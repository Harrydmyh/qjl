import { useEffect } from 'react';

export interface DialogState {
  title: string;
  msg: string;
  withCancel: boolean;
  resolve: (v: boolean) => void;
}

/**
 * 页内弹窗。
 * ⚠️ 刻意不用原生 alert/confirm：在嵌入式预览面板里原生弹窗会被静默吞掉，
 * 表现为「点了按钮没反应」—— 这个坑踩过。
 */
export default function Dialog({ state, onClose }: { state: DialogState | null; onClose: (v: boolean) => void }) {
  useEffect(() => {
    if (!state) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        onClose(true);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        onClose(false);
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [state, onClose]);

  if (!state) return null;
  return (
    <div className="overlay" onClick={() => onClose(false)}>
      <div className="dialog" onClick={(e) => e.stopPropagation()}>
        <h3>{state.title}</h3>
        <p>{state.msg}</p>
        <div className="actions">
          {state.withCancel && (
            <button className="btn btn-ghost" onClick={() => onClose(false)}>
              取消
            </button>
          )}
          <button className="btn btn-primary" autoFocus onClick={() => onClose(true)}>
            确定
          </button>
        </div>
      </div>
    </div>
  );
}
