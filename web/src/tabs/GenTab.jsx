import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { usePoll } from "../App.jsx";
import { Card, ErrBox, LoadBox } from "./OverviewTab.jsx";

export default function GenTab() {
  const deployments = usePoll(api.deployments, [], 5000);
  const ready = (deployments?.v || []).filter((d) => d.state === "ready");
  const [depId, setDepId] = useState("");
  const [ckpt, setCkpt] = useState("");
  const [ckpts, setCkpts] = useState([]);
  const [form, setForm] = useState({
    prompt: "", negative: "", width: 1024, height: 1024, steps: 25, seed: 0,
  });
  const [mode, setMode] = useState("txt2img"); // txt2img | raw
  const [raw, setRaw] = useState("");
  const [assets, setAssets] = useState([]);
  const [status, setStatus] = useState(""); // 轮询文案
  const [err, setErr] = useState("");
  const timer = useRef(null);

  useEffect(() => { setDepId(""); setCkpts([]); setCkpt(""); setAssets([]); }, [ready.length]);

  const pick = async (id) => {
    setDepId(id); setErr(""); setStatus(""); setCkpt(""); setCkpts([]);
    if (!id) return;
    try {
      setAssets(await api.genAssets(id));
    } catch (e) { setErr(e.message); }
    if (id) {
      api.genCheckpoints(id)
        .then((r) => { setCkpts(r.checkpoints || []); })
        .catch(() => setCkpts([]));  // 未开隧道/非 comfyui 时静默
    }
  };

  const generate = async (e) => {
    e.preventDefault();
    if (!depId) return;
    setErr(""); setStatus("提交中…");
    try {
      const { prompt_id } = await api.genSubmit(depId, {
        ...form, seed: Number(form.seed) || 0,
        width: Number(form.width), height: Number(form.height),
        steps: Number(form.steps), checkpoint: ckpt,
        template: mode, raw_workflow: raw,
      });
      poll(prompt_id);
    } catch (e2) { setErr(e2.message); setStatus(""); }
  };

  const poll = async (promptId) => {
    clearInterval(timer.current);
    timer.current = setInterval(async () => {
      try {
        const r = await api.genHistory(depId, promptId, form.prompt);
        if (r.state === "pending") { setStatus("生成中…（Colab GPU 上执行）"); return; }
        clearInterval(timer.current);
        if (r.state === "done") {
          setStatus(`完成：${r.assets.length} 个产物`);
          setAssets(await api.genAssets(depId));
        } else {
          setErr("生成失败（查看部署日志）");
          setStatus("");
        }
      } catch (e) { clearInterval(timer.current); setErr(e.message); setStatus(""); }
    }, 3000);
    setStatus("已提交，等待调度…");
  };

  useEffect(() => () => clearInterval(timer.current), []);

  if (!deployments) return <LoadBox />;
  return (
    <>
      {err && <ErrBox text={err} />}
      <div className="cards">
        <Card title="生成（图片 / 视频）">
          {ready.length === 0 ? (
            <p className="dim">
              没有就绪的部署。用「部署」页启动 builtin/comfyui（生图/生视频）或
              builtin/llamacpp-chat（聊天）后回来。
            </p>
          ) : (
            <div className="row">
              <select value={depId} onChange={(e) => pick(e.target.value)}>
                <option value="">选择就绪部署…</option>
                {ready.map((d) => <option key={d.id} value={d.id}>#{d.id}</option>)}
              </select>
              {!ready.find((d) => String(d.id) === String(depId))?.params?.endpoint &&
                depId && (
                  <span className="dim">（未开隧道；到「部署」页点「开隧道」）</span>
                )}
            </div>
          )}

          {depId && (
            <form onSubmit={generate} className="grid">
              <div className="row">
                <button type="button" className={mode === "txt2img" ? "chip on" : "chip"}
                        onClick={() => setMode("txt2img")}>文生图（内置模板）</button>
                <button type="button" className={mode === "raw" ? "chip on" : "chip"}
                        onClick={() => setMode("raw")}>工作流（生视频/进阶）</button>
              </div>

              {mode === "txt2img" && (
                <select value={ckpt} onChange={(e) => setCkpt(e.target.value)} required>
                  <option value="">选择 checkpoint{ckpts.length ? "" : "（无：确认 ComfyUI 已装配）"}</option>
                  {ckpts.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              )}
              <textarea placeholder="提示词 Prompt" value={form.prompt} rows={3}
                        onChange={(e) => setForm({ ...form, prompt: e.target.value })} required />
              <textarea placeholder="负面提示词（可空）" value={form.negative} rows={2}
                        onChange={(e) => setForm({ ...form, negative: e.target.value })} />
              {mode === "txt2img" && (
                <div className="row">
                  <input type="number" placeholder="宽" value={form.width}
                         onChange={(e) => setForm({ ...form, width: e.target.value })} />
                  <input type="number" placeholder="高" value={form.height}
                         onChange={(e) => setForm({ ...form, height: e.target.value })} />
                  <input type="number" placeholder="步数" value={form.steps}
                         onChange={(e) => setForm({ ...form, steps: e.target.value })} />
                  <input type="number" placeholder="种子(0=随机)" value={form.seed}
                         onChange={(e) => setForm({ ...form, seed: e.target.value })} />
                </div>
              )}
              {mode === "raw" && (
                <>
                  <textarea placeholder='ComfyUI API 格式 workflow JSON（WAN 官方模板导出后粘这里；支持 {{prompt}} {{negative}} {{ckpt}} {{seed}} {{width}} {{height}} {{steps}} 变量）'
                            value={raw} rows={8}
                            onChange={(e) => setRaw(e.target.value)} required />
                  <p className="dim">
                    生视频：ComfyUI 网页里搭好 WAN 工作流 → 「导出（API Format）」→ 粘贴。
                  </p>
                </>
              )}
              <button type="submit" disabled={!form.prompt.trim()}>
                {status || "生成"}
              </button>
            </form>
          )}
        </Card>

        {depId && (
          <Card title={`产物（${assets.length}）`}>
            {assets.length === 0 ? <p className="dim">还没有产物</p> : (
              <div className="gal">
                {assets.map((a) => (
                  <figure key={a.id} className="thumb">
                    {a.kind === "video"
                      ? <video src={api.genAssetFileUrl(a.id)} controls preload="metadata" />
                      : <img src={api.genAssetFileUrl(a.id)} alt={a.prompt} loading="lazy" />}
                    <figcaption>
                      {a.kind === "video" ? "🎬" : "🖼"} {a.filename}
                      <button className="mini" onClick={() =>
                        api.genDeleteAsset(a.id).then(() => pick(depId))}>删</button>
                    </figcaption>
                  </figure>
                ))}
              </div>
            )}
          </Card>
        )}
      </div>
    </>
  );
}
