import { useState } from 'react';
import { api } from '../api';

export function OutputSettings({ initial }: { initial: string }) {
  const [path, setPath] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  return (
    <form
      className="output-settings settings-section"
      onSubmit={async (event) => {
        event.preventDefault();
        setBusy(true);
        setError('');
        setSaved(false);
        try {
          const result = await api.settings(path);
          setPath(result.output_root);
          setSaved(true);
        } catch (e) {
          setError(e instanceof Error ? e.message : '保存失败');
        } finally {
          setBusy(false);
        }
      }}
    >
      <div>
        <h3>任务文件目录</h3>
      </div>
      <p>未指定工作区时使用此位置，每个会话单独保存。修改后对新会话生效。</p>
      <label className="field-label">
        保存目录
        <input
          value={path}
          onChange={(event) => {
            setPath(event.target.value);
            setSaved(false);
          }}
        />
      </label>
      <div className="output-settings-actions">
        {error && <span role="alert">{error}</span>}
        {saved && <span role="status">已保存，新会话将使用此位置。</span>}
        <button className="primary" disabled={busy || !path.trim()}>
          {busy ? '正在保存…' : '保存位置'}
        </button>
      </div>
    </form>
  );
}
