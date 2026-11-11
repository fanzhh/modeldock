import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { usePoll } from "../App.jsx";
import { Card, ErrBox, LoadBox } from "./OverviewTab.jsx";

export default function ChatTab() {
  const deployments = usePoll(api.deployments, [], 4000);
  const ready = (deployments?.v || []).filter((d) => d.state === "ready");
  const [depId, setDepId] = useState("");
  const [threads, setThreads] = useState([]);
  const [threadId, setThreadId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [endpoint, setEndpoint] = useState("http://127.0.0.1:18080");
  const [err, setErr] = useState("");
  const bottom = useRef(null);

  const refreshThreads = async () => {
    if (!depId) { setThreads([]); return; }
    try { setThreads(await api.threads(Number(depId))); }
    catch (e) { setErr(e.message); }
  };
  useEffect(() => { setThreadId(null); setMessages([]); refreshThreads(); }, [depId]);

  // 部署参数里已有端点（开隧道时自动登记）→ 直接用它，无需手动填
  useEffect(() => {
    const dep = ready.find((d) => String(d.id) === String(depId));
    if (dep?.params?.chat_endpoint) setEndpoint(dep.params.chat_endpoint);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [depId]);

  const openThread = async (id) => {
    setThreadId(id);
    try {
      const t = await api.thread(id);
      setMessages(t.messages);
    } catch (e) { setErr(e.message); }
  };

  const newThread = async () => {
    try {
      const t = await api.createThread(Number(depId), { title: `chat ${new Date().toLocaleString()}` });
      await refreshThreads();
      setThreadId(t.id);
      setMessages([]);
    } catch (e) { setErr(e.message); }
  };

  const saveEndpoint = async () => {
    try { await api.setChatEndpoint(Number(depId), endpoint); setErr(""); }
    catch (e) { setErr(e.message); }
  };

  const send = async (e) => {
    e.preventDefault();
    if (!input.trim() || !threadId || streaming) return;
    const text = input.trim();
    setInput("");
    setMessages((m) => [...m, { role: "user", content: text }]);
    setStreaming(true);
    setMessages((m) => [...m, { role: "assistant", content: "" }]);
    try {
      await api.sendMessageStream(threadId, text, (obj) => {
        if (obj.error) { setErr(obj.error); return; }
        for (const ch of obj.choices || []) {
          const delta = ch.delta?.content || "";
          if (delta) setMessages((m) => {
            const copy = [...m];
            copy[copy.length - 1] = {
              ...copy[copy.length - 1],
              content: (copy[copy.length - 1].content || "") + delta,
            };
            return copy;
          });
        }
      });
    } catch (e2) {
      setErr(e2.message);
    } finally {
      setStreaming(false);
      // 与服务端保存的历史对齐（拿权威版本）
      try { const t = await api.thread(threadId); setMessages(t.messages); }
      catch { /* 保留本地回显 */ }
    }
  };

  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  if (!deployments) return <LoadBox />;
  return (
    <>
      {err && <ErrBox text={err} />}
      <div className="cards">
        <Card title="选择就绪的部署">
          {ready.length === 0 ? <p className="dim">没有 ready 的部署。先到「部署」页启动聊天配方。</p> : (
            <div className="row">
              <select value={depId} onChange={(e) => setDepId(e.target.value)}>
                <option value="">选择部署…</option>
                {ready.map((d) => <option key={d.id} value={d.id}>#{d.id}</option>)}
              </select>
              {depId && <button onClick={newThread}>新对话</button>
              }
            </div>
          )}
          {depId && (
            <div className="row" style={{ marginTop: 8 }}>
              <input placeholder="本地隧道端点，如 http://127.0.0.1:18080"
                     value={endpoint} onChange={(e) => setEndpoint(e.target.value)} />
              <button onClick={saveEndpoint}>登记</button>
              <span className="dim">
                （隧道：ssh -L 18080:127.0.0.1:8080 …colab ssh）
              </span>
            </div>
          )}
        </Card>

        {depId && threads.length > 0 && (
          <Card title="历史对话">
            <div className="chips">
              {threads.map((t) => (
                <button key={t.id} className={threadId === t.id ? "chip on" : "chip"}
                        onClick={() => openThread(t.id)}>{t.title}</button>
              ))}
            </div>
          </Card>
        )}

        {threadId && (
          <Card title="对话">
            <div className="chatlog">
              {messages.length === 0 && <p className="dim">说点什么吧…</p>}
              {messages.map((m, i) => (
                <div key={i} className={m.role === "user" ? "msg user" : "msg bot"}>
                  {m.content}
                </div>
              ))}
              <div ref={bottom} />
            </div>
            <form onSubmit={send} className="row" style={{ marginTop: 10 }}>
              <input placeholder={streaming ? "生成中…" : "输入消息（Enter 发送）"}
                     value={input} onChange={(e) => setInput(e.target.value)}
                     disabled={streaming} />
              <button type="submit" disabled={streaming || !input.trim()}>发送</button>
            </form>
          </Card>
        )}
      </div>
    </>
  );
}
