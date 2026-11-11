import { useState } from "react";
import { api } from "../api.js";
import { usePoll } from "../App.jsx";
import { Card, ErrBox, LoadBox } from "./OverviewTab.jsx";

export default function ModelsTab() {
  const families = usePoll(api.families, []);
  const tasks = usePoll(api.tasks, []);
  const [err, setErr] = useState("");
  const [fam, setFam] = useState({ name: "", purpose: "chat" });
  const [task, setTask] = useState({ family_id: "", source: "hf", repo: "", filename: "", target_remote_path: "" });
  const [token, setToken] = useState({ scope: "hf", secret: "" });

  const submitFam = async (e) => {
    e.preventDefault();
    try { await api.createFamily(fam); setFam({ ...fam, name: "" }); }
    catch (e) { setErr(e.message); }
  };
  const submitTask = async (e) => {
    e.preventDefault();
    try {
      await api.createTask({ ...task, family_id: Number(task.family_id) });
      setTask({ ...task, repo: "", filename: "", target_remote_path: "" });
    } catch (e) { setErr(e.message); }
  };
  const submitToken = async (e) => {
    e.preventDefault();
    try { await api.putSecret({ scope: token.scope, name: "main", secret: token.secret, meta: {} }); setToken({ ...token, secret: "" }); }
    catch (e) { setErr(e.message); }
  };

  if (!families || !tasks) return <LoadBox />;
  return (
    <>
      {err && <ErrBox text={err} />}
      <div className="cards">
        <Card title="下载源 token（vault 加密，只写不读）">
          <form onSubmit={submitToken} className="row">
            <select value={token.scope} onChange={(e) => setToken({ ...token, scope: e.target.value })}>
              <option value="hf">HuggingFace</option>
              <option value="modelscope">魔搭</option>
            </select>
            <input type="password" placeholder="hf_xxx / ms-xxx" value={token.secret}
                   onChange={(e) => setToken({ ...token, secret: e.target.value })} required />
            <button type="submit">存入</button>
          </form>
        </Card>

        <Card title="新建模型家族">
          <form onSubmit={submitFam} className="row">
            <input placeholder="如 Qwen3-8B" value={fam.name}
                   onChange={(e) => setFam({ ...fam, name: e.target.value })} required />
            <select value={fam.purpose} onChange={(e) => setFam({ ...fam, purpose: e.target.value })}>
              <option value="chat">聊天</option>
              <option value="image">图像</option>
              <option value="video">视频</option>
              <option value="audio">音频</option>
              <option value="generic">通用</option>
            </select>
            <button type="submit">添加</button>
          </form>
        </Card>

        <Card title="排队下载 → 云盘">
          <form onSubmit={submitTask} className="grid">
            <select value={task.family_id} onChange={(e) => setTask({ ...task, family_id: e.target.value })} required>
              <option value="">选择家族…</option>
              {families.v.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
            </select>
            <select value={task.source} onChange={(e) => setTask({ ...task, source: e.target.value })}>
              <option value="hf">HuggingFace</option>
              <option value="modelscope">魔搭</option>
            </select>
            <input placeholder="org/name（如 Qwen/Qwen3-8B-GGUF）" value={task.repo}
                   onChange={(e) => setTask({ ...task, repo: e.target.value })} />
            <input placeholder="文件名（如 qwen3-8b-q4_k_m.gguf）" value={task.filename}
                   onChange={(e) => setTask({ ...task, filename: e.target.value })} />
            <input placeholder="云盘相对路径（如 qwen3/q4.gguf）" value={task.target_remote_path}
                   onChange={(e) => setTask({ ...task, target_remote_path: e.target.value })} required />
            <button type="submit">排队</button>
          </form>
        </Card>

        <Card title={`家族（${families.v.length}）`}>
          {families.v.length === 0 ? <p className="dim">还没有模型家族</p> : (
            <ul>{families.v.map((f) => <li key={f.id}><b>{f.name}</b> · {f.purpose}</li>)}</ul>
          )}
        </Card>

        <Card title={`下载任务（${tasks.v.length}）`}>
          {tasks.v.length === 0 ? <p className="dim">队列为空</p> : (
            <ul>{tasks.v.map((t) => (
              <li key={t.id}>
                #{t.id} [{t.source}] {t.filename || t.repo} → <b>{t.status}</b>
                {t.total_bytes > 0 && <em> {Math.round((t.done_bytes / t.total_bytes) * 100)}%</em>}
              </li>
            ))}</ul>
          )}
        </Card>
      </div>
    </>
  );
}
