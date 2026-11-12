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
  const registered = !!d?.registered;
  return (
    <Card title="Google Drive">
      {drive?.ok === false && <p className="dim">—</p>}
      {d && (
        <>
          托管 remote <b>{d.managed_remote}</b>：
          {registered
            ? (d.health?.ok
                ? <b>已注册 ✅</b>
                : <span>
                    <b>已注册 ⚠️ 健康检查未过</b>
                    <div className="block">
                      <pre className="dim">{d.health?.detail || "unknown error"}</pre>
                      <p className="dim">
                        常见原因：授权时 Google 未签发 refresh_token（旧授权未撤销时不会重复签发）。
                        到{" "}
                        <a href="https://myaccount.google.com/permissions"
                           target="_blank" rel="noreferrer">Google 账号 → 第三方访问</a>{" "}
                        撤销 ModelDock 的授权，然后断开重新走一遍向导（页面会带 prompt=consent）。
                      </p>
                    </div>
                  </span>)
            : <b>未连接</b>}
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
          {(!registered || d.health?.ok === false) && (
            <div className="block">
              <h3>连接向导（Google Cloud Console OAuth）</h3>
              <ol className="steps">
                <li className="dim">
                  若之前授权失败或重复授权过：先到{" "}
                  <a href="https://myaccount.google.com/permissions"
                     target="_blank" rel="noreferrer">Google 账号 → 第三方访问</a>{" "}
                  撤销本应用授权（否则 Google 不再签发 refresh_token，连接会失败）
                </li>
                <li>
                  打开{" "}
                  <a href="https://console.cloud.google.com/apis/credentials"
                     target="_blank" rel="noreferrer">Google Cloud Console → 凭据</a>{" "}
                  → 「创建凭据 → OAuth 客户端 ID」，类型选 <b>Web 应用</b>
                </li>
                <li>
                  点「开始授权」→ 本页会显示你环境的<b>准确 redirect URI</b>，
                  复制到 Console 的「已获授权的重定向 URI」保存
                </li>
                <li>把生成的 client_id / client_secret 填进下面，点「开始授权」</li>
                <li>打开授权链接、批准权限——成功后本卡片自动变为已注册</li>
              </ol>
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
                  <p>① 把下面的 redirect URI 加进 Console 的 OAuth 客户端（Web 应用需 HTTPS）；「authorized origins」可留空：</p>
                  <pre><input readOnly value={oauth.redirect_uri} onFocus={(ev) => ev.target.select()} /></pre>
                  <p>② 浏览器打开授权链接、批准权限（多账号注意选对账号）：</p>
                  <p><a href={oauth.authorize_url} target="_blank" rel="noreferrer">{oauth.authorize_url}</a></p>
                  <p className="dim">授权后 Google 会跳回本站自动完成注册。</p>
                </div>
              )}
            </div>
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
  const [done, setDone] = useState(null);     // {output} 登录成功
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");

  const loggedIn = usage?.ok && usage.v?.logged_in;

  const start = async () => {
    setErr(""); setCode(""); setDone(null); setBusy("start"); setFlow(null);
    try {
      const r = await api.colabAuthStart();
      if (r.auth_required) setFlow(r); else setDone({ output: r.usage, already: true });
    } catch (e) {
      if (/进行中/.test(e.message)) {
        // 旧会话悬挂 → 自动放弃重开（单按钮自愈）
        await api.colabAuthCancel().catch(() => {});
        try { setFlow(await api.colabAuthStart()); setBusy(""); return; }
        catch (e2) { setErr(e2.message); }
      } else setErr(e.message);
    }
    setBusy("");
  };
  const submit = async (e) => {
    e.preventDefault(); setErr(""); setBusy("submit");
    try {
      const r = await api.colabAuthSubmit(code);
      if (r.ok === false) { setErr(`登录失败：${r.output || ""}`.slice(0, 300)); }
      else { setFlow(null); setDone({ output: r.output, already: false }); }
    } catch (e2) { setErr(e2.message); }
    setBusy("");
  };

  return (
    <Card title="Colab（计算额度）">
      {(loggedIn || done) && (
        <div className="block">
          <p>✅ <b>已登录</b>（colab CLI 已存好凭证）</p>
          {done && <pre>{done.output || usage?.v?.usage || ""}</pre>}
          {!done && usage?.v?.usage && <pre>{usage.v.usage}</pre>}
        </div>
      )}
      {(!loggedIn && !done) && (
        (usage?.ok && !usage.v.logged_in)
          ? <p className="dim">CLI 未登录——Colab 部署与隧道需要先完成一次浏览器授权</p>
          : usage?.ok === false ? <p className="dim">{usage.e}</p> : ""
      )}
      {flow?.auth_required ? (
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
            <button type="button" disabled={!!busy} onClick={async () => {
              await api.colabAuthCancel().catch(() => {});
              setFlow(null);
            }}>放弃</button>
          </form>
        </div>
      ) : (
        <button disabled={!!busy} onClick={start}>
          {busy === "start" ? "启动授权…" : (loggedIn ? "重新检测登录" : "登录 Colab（浏览器授权）")}
        </button>
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
