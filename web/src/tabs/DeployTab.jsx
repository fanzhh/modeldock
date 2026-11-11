import { useState } from "react";
import { api } from "../api.js";
import { usePoll } from "../App.jsx";
import { Card, ErrBox, LoadBox } from "./OverviewTab.jsx";

const STATE_LABEL = {
  queued: "排队", provision: "开机中", install: "安装中", syncing_models: "装配模型",
  booting: "启动服务", ready: "✅ 就绪", releasing: "释放中", done: "已完成",
  failed: "❌ 失败", cancelled: "已取消",
};

export default function DeployTab() {
  const recipes = usePoll(api.recipes, []);
  const deployments = usePoll(api.deployments, [], 2500);
  const [err, setErr] = useState("");
  // 内置配方的 family 模板 → 展开的输入框
  const [form, setForm] = useState({ recipe_id: "", gpu: "T4", families: "{}", idle_timeout_min: "" });
  const [logsFor, setLogsFor] = useState(null);
  const [logsText, setLogsText] = useState("");

  const selectRecipe = (id) => {
    const rec = recipes?.v?.find((r) => r.id === Number(id));
    const models = rec?.manifest?.models || {};
    const defaults = {};
    for (const [role, spec] of Object.entries(models)) {
      if (String(spec.family || "").startsWith("$")) defaults[role.slice(1)] = "";
    }
    setForm({ ...form, recipe_id: id, families: JSON.stringify(defaults) });
  };

  const submit = async (e) => {
    e.preventDefault();
    try {
      let families;
      try { families = JSON.parse(form.families || "{}"); } catch { throw new Error("families 必须是 JSON"); }
      const params = { families };
      if (form.idle_timeout_min) params.idle_timeout_min = Number(form.idle_timeout_min);
      const d = await api.createDeployment({ recipe_id: Number(form.recipe_id), gpu: form.gpu, params });
      await api.startDeployment(d.id);
    } catch (e2) { setErr(e2.message); }
  };

  const act = async (fn, id) => { try { await fn(id); } catch (e) { setErr(e.message); } };
  const showLogs = async (id) => { setLogsFor(id); setLogsText(await api.deploymentLogs(id)); };

  if (!recipes || !deployments) return <LoadBox />;
  return (
    <>
      {err && <ErrBox text={err} />}
      <div className="cards">
        <Card title="新建部署">
          <form onSubmit={submit} className="grid">
            <select value={form.recipe_id} onChange={(e) => selectRecipe(e.target.value)} required>
              <option value="">选择配方…</option>
              {recipes.v.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}{r.manifest?.builtin ? "（内置）" : ""} · {r.kind_class}
                </option>
              ))}
            </select>
            <select value={form.gpu} onChange={(e) => setForm({ ...form, gpu: e.target.value })}>
              {["T4", "L4", "G4", "H100", "A100"].map((g) => <option key={g}>{g}</option>)}
            </select>
            <input placeholder='families JSON，如 {"chat": "Qwen3-8B"}' value={form.families}
                   onChange={(e) => setForm({ ...form, families: e.target.value })} />
            <input placeholder="空闲自动释放（分钟，0=不自动）" value={form.idle_timeout_min}
                   onChange={(e) => setForm({ ...form, idle_timeout_min: e.target.value })} />
            <button type="submit">部署并启动</button>
          </form>
        </Card>

        <Card title={`部署（${deployments.v.length}）`}>
          {deployments.v.length === 0 ? <p className="dim">还没有部署</p> : (
            <table className="tbl">
              <thead><tr><th>#</th><th>配方</th><th>GPU</th><th>状态</th><th>操作</th></tr></thead>
              <tbody>
                {deployments.v.map((d) => {
                  const rec = recipes.v.find((r) => r.id === d.recipe_id);
                  const parked = ["done", "failed", "cancelled"].includes(d.state);
                  return (
                    <tr key={d.id}>
                      <td>{d.id}</td>
                      <td>{rec?.name || d.recipe_id}</td>
                      <td>{d.params?.session?.gpu || d.gpu}</td>
                      <td><b>{STATE_LABEL[d.state] || d.state}</b>
                        {d.params?.tunnel && <em> · 隧道:{d.params.tunnel.local_port}→:{d.params.tunnel.service_port}</em>}
                      </td>
                      <td className="ops">
                        {!parked && d.state !== "ready" && <button onClick={() => act(api.cancelDeployment, d.id)}>取消</button>}
                        {parked && <button onClick={() => act(api.startDeployment, d.id)}>重跑</button>}
                        {d.state === "ready" && (d.params?.tunnel
                          ? <button onClick={() => act(api.tunnelStop, d.id)}>关隧道</button>
                          : <button onClick={() => act(api.tunnelStart, d.id)}>开隧道</button>)}
                        {d.state === "ready" && <button onClick={() => act(api.releaseDeployment, d.id)}>释放</button>}
                        <button onClick={() => showLogs(d.id)}>日志</button>
                        <button onClick={() => {
                          api.notebook(d.id).then((t) => {
                            const blob = new Blob([t], { type: "text/x-python" });
                            const a = document.createElement("a");
                            a.href = URL.createObjectURL(blob);
                            a.download = `modeldock-dep${d.id}.py`;
                            a.click();
                          });
                        }}>兜底单元格</button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Card>

        {logsFor !== null && (
          <Card title={`部署 #${logsFor} 日志`}>
            <pre className="logs">{logsText || "（空）"}</pre>
            <button onClick={async () => setLogsText(await api.deploymentLogs(logsFor))}>刷新</button>
          </Card>
        )}
      </div>
    </>
  );
}
