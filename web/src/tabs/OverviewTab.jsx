import { useState } from "react";
import { api } from "../api.js";
import { usePoll } from "../App.jsx";

export default function OverviewTab() {
  const health = usePoll(api.health, []);
  const drive = usePoll(api.driveStatus, []);
  const usage = usePoll(api.colabUsage, []);
  const secrets = usePoll(api.secrets, []);

  return (
    <div className="cards">
      {health?.ok === false && <ErrBox text={health.e} />}
      <Card title="服务">
        {health?.ok ? (
          <ul>
            <li>状态 <b>运行中</b> · v{health.v.version}</li>
            <li>Drive: {health.v.drive?.ok ? "✅ 健康" : `⚠️ 未就绪（见下）`}</li>
          </ul>
        ) : <LoadBox />}
      </Card>
      <DriveCard drive={drive} />
      <ColabCard usage={usage} />
      <Card title="凭证（只有指纹元数据）">
        {secrets?.ok && secrets.v.length > 0 ? (
          <ul>{secrets.v.map((s) => (
            <li key={s.id}><b>{s.scope}</b> / {s.name} · fp {s.fingerprint}</li>
          ))}</ul>
        ) : <p className="dim">HuggingFace / 魔搭 token 在「模型仓库」页录入</p>}
      </Card>
    </div>
  );
}

function DriveCard({ drive }) {
  const [form, setForm] = useState({ client_id: "", client_secret: "" });
  const [oauth, setOauth] = useState(null);   // {authorize_url, redirect_uri}
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");

  const importRemote = async () => {
    setBusy("import"); setErr("");
    try { await api.driveImport(); }
    catch (e) { setErr(e.message); }
    setBusy("");
  };
  const startOauth = async (e) => {
    e.preventDefault(); setErr(""); setBusy("oauth"); setOauth(null);
    try { setOauth(await api.driveOauthStart(form)); }
    catch (e2) { setErr(e2.message); }
    setBusy("");
  };

  const d = drive?.ok ? drive.v : null;
  return (
    <Card title="Google Drive">
      {drive?.ok === false && <p className="dim">—</p>}
      {d && (
        <>
          托管 remote <b>{d.managed_remote}</b>：{d.registered
            ? <b>已注册 ✅ {d.health?.ok ? "" : "（健康检查未过）"}</b>
            : "未注册"}
          {d.importable?.length > 0 && (
            <div className="block">
              <p className="dim">
                检测到本机 rclone remote：{d.importable.map((i) => i.name).join(", ")}
              </p>
              <button disabled={!!busy} onClick={importRemote}>
                {busy === "import" ? "导入中…" : "一键导入"}
              </button>
            </div>
          )}
          {!d.registered && (
            <details className="block">
              <summary>没有现成 remote？用 OAuth 向导（需 Google Cloud Console 的 OAuth 客户端）</summary>
              <form onSubmit={startOauth} className="grid">
                <input placeholder="client_id（…apps.googleusercontent.com）"
                       value={form.client_id}
                       onChange={(e) => setForm({ ...form, client_id: e.target.value })} required />
                <input type="password" placeholder="client_secret（GOCSPX-…）"
                       value={form.client_secret}
                       onChange={(e) => setForm({ ...form, client_secret: e.target.value })} required />
                <button disabled={!!busy} type="submit">
                  {busy === "oauth" ? "生成授权链接…" : "开始授权"}
                </button>
              </form>
              {oauth && (
                <div className="block">
                  <p>① 把下面的 redirect URI 加进 Google Cloud Console 的 OAuth 客户端（Web 应用需 HTTPS）：</p>
                  <pre>{oauth.redirect_uri}</pre>
                  <p>② 浏览器打开授权链接、批准权限：</p>
                  <p><a href={oauth.authorize_url} target="_blank" rel="noreferrer">{oauth.authorize_url}</a></p>
                  <p className="dim">授权后 Google 会跳回本站自动完成注册。</p>
                </div>
              )}
            </details>
          )}
          {err && <ErrBox text={err} />}
        </>
      )}
    </Card>
  );
}

function ColabCard({ usage }) {
  const [flow, setFlow] = useState(null);    // start 响应
  const [code, setCode] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");

  const start = async () => {
    setErr(""); setCode(""); setBusy("start"); setFlow(null);
    try { setFlow(await api.colabAuthStart()); }
    catch (e) { setErr(e.message); }
    setBusy("");
  };
  const submit = async (e) => {
    e.preventDefault(); setErr(""); setBusy("submit");
    try {
      const r = await api.colabAuthSubmit(code);
      if (r.ok === false) setErr(`CLI 返回失败：${r.output || ""}`.slice(0, 300));
      setFlow(null);
    } catch (e2) { setErr(e2.message); }
    setBusy("");
  };

  return (
    <Card title="Colab（计算额度）">
      {usage?.ok && <pre>{JSON.stringify(usage.v, null, 2)}</pre>}
      {!usage?.ok && <p className="dim">CLI 未登录或后端为 mock</p>}
      {!flow ? (
        <button disabled={!!busy} onClick={start}>
          {busy === "start" ? "启动授权…" : "登录 Colab（浏览器授权）"}
        </button>
      ) : flow.auth_required ? (
        <div className="block">
          <p>① 浏览器打开链接、在 Google 账户上批准 Colab CLI：</p>
          <p><a href={flow.url} target="_blank" rel="noreferrer">{flow.url}</a></p>
          <p>② 把 Google 给出的授权码粘到下面：</p>
          <form onSubmit={submit} className="row">
            <input placeholder="4/0A…授权码" value={code}
                   onChange={(e) => setCode(e.target.value)} required />
            <button disabled={!!busy} type="submit">
              {busy === "submit" ? "提交中…" : "提交"}
            </button>
          </form>
        </div>
      ) : (
        <div className="block">
          <p className="dim">已登录，余额输出：</p>
          <pre>{flow.usage || ""}</pre>
        </div>
      )}
      {err && <ErrBox text={err} />}
    </Card>
  );
}

export function LoadBox() { return <p className="dim">加载中…</p>; }
export function ErrBox({ text }) { return <p className="err">{text}</p>; }
export function Card({ title, children }) {
  return (
    <section>
      <h2>{title}</h2>
      {children}
    </section>
  );
}
