import { api } from "../api.js";
import { usePoll } from "../App.jsx";

export default function OverviewTab() {
  const health = usePoll(api.health, []);
  const drive = usePoll(api.driveStatus, []);
  const usage = usePoll(api.colabUsage, []);
  const secrets = usePoll(api.secrets, []);

  if (!health) return <LoadBox />;
  return (
    <>
      {health.ok === false && <ErrBox text={health.e} />}
      <div className="cards">
        <Card title="服务">
          {health.ok ? (
            <ul>
              <li>状态 <b>运行中</b> · v{health.v.version}</li>
              <li>Drive: {health.v.drive?.ok ? "✅ 健康" : `⚠️ ${health.v.drive?.detail || "未注册"}`}</li>
            </ul>
          ) : (
            <p className="dim">连不上 server：先启动 <code>uv run python -m uvicorn app.main:app</code></p>
          )}
        </Card>
        <Card title="Google Drive">
          {drive?.ok ? (
            <ul>
              <li>托管 remote <b>{drive.v.managed_remote}</b>：{drive.v.registered ? "已注册" : "未注册"}</li>
              {drive.v.importable?.length > 0 && (
                <li className="dim">
                  发现本机 remote：{drive.v.importable.map((i) => i.name).join(", ")}
                  （导入：POST /api/accounts/drive/import）
                </li>
              )}
            </ul>
          ) : <p className="dim">—</p>}
        </Card>
        <Card title="Colab 余额">
          {usage?.ok ? <pre>{JSON.stringify(usage.v, null, 2)}</pre> : <p className="dim">—</p>}
        </Card>
        <Card title="凭证（只有指纹元数据）">
          {secrets?.ok && secrets.v.length > 0 ? (
            <ul>{secrets.v.map((s) => (
              <li key={s.id}><b>{s.scope}</b> / {s.name} · fp {s.fingerprint}</li>
            ))}</ul>
          ) : <p className="dim">未配置 OAuth 之外的 token；PUT /api/accounts/secrets</p>}
        </Card>
      </div>
    </>
  );
}

export function LoadBox() { return <p className="dim">加载中…</p>; }
export function ErrBox({ text }) {
  return <p className="err">{text}</p>;
}
export function Card({ title, children }) {
  return (
    <section>
      <h2>{title}</h2>
      {children}
    </section>
  );
}
