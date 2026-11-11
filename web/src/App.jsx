import { useEffect, useState } from "react";

const api = {
  health: () => fetch("/api/health").then((r) => r.json()),
  secrets: () => fetch("/api/accounts/secrets").then((r) => r.json()),
  families: () => fetch("/api/models/families").then((r) => r.json()),
  tasks: () => fetch("/api/models/tasks").then((r) => r.json()),
};

export default function App() {
  const [health, setHealth] = useState(null);
  const [secrets, setSecrets] = useState([]);
  const [families, setFamilies] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [err, setErr] = useState("");

  const refresh = async () => {
    try {
      setErr("");
      const [h, s, f, t] = await Promise.all([
        api.health(), api.secrets(), api.families(), api.tasks(),
      ]);
      setHealth(h); setSecrets(s); setFamilies(f); setTasks(t);
    } catch (e) {
      setErr(`无法连接 server（先启动: cd server && uv run python -m uvicorn app.main:app --port 8820）: ${e}`);
    }
  };

  useEffect(() => { refresh(); const id = setInterval(refresh, 3000); return () => clearInterval(id); }, []);

  return (
    <main className="wrap">
      <header>
        <h1>ModelDock <small>dev console</small></h1>
        <p className="sub">M0 骨架盘 · 完整原型见 ../design/prototype.html</p>
      </header>
      {err && <p className="err">{err}</p>}

      <section>
        <h2>服务</h2>
        <pre>{JSON.stringify(health, null, 2)}</pre>
      </section>

      <section>
        <h2>凭证（只显示指纹元数据）</h2>
        <ul>
          {secrets.length === 0 && <li className="dim">未配置。PUT /api/accounts/secrets</li>}
          {secrets.map((s) => (
            <li key={s.id}>
              <b>{s.scope}</b> / {s.name} · fp {s.fingerprint}
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h2>模型家族</h2>
        <ul>
          {families.length === 0 && <li className="dim">还没有模型。POST /api/models/families</li>}
          {families.map((f) => (
            <li key={f.id}><b>{f.name}</b> · {f.purpose}</li>
          ))}
        </ul>
      </section>

      <section>
        <h2>下载任务</h2>
        <ul>
          {tasks.length === 0 && <li className="dim">队列为空</li>}
          {tasks.map((t) => (
            <li key={t.id}>
              #{t.id} [{t.source}] {t.filename || t.repo} → {t.status}
              {t.total_bytes > 0 && <em> {Math.round((t.done_bytes / t.total_bytes) * 100)}%</em>}
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
