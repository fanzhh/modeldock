import { useEffect, useState } from "react";
import OverviewTab from "./tabs/OverviewTab.jsx";
import ModelsTab from "./tabs/ModelsTab.jsx";
import DeployTab from "./tabs/DeployTab.jsx";
import ChatTab from "./tabs/ChatTab.jsx";

const TABS = [
  ["overview", "总览"],
  ["models", "模型仓库"],
  ["deploy", "部署"],
  ["chat", "Chat"],
];

export default function App() {
  const [tab, setTab] = useState("overview");

  return (
    <main className="wrap">
      <header>
        <h1>
          ModelDock <small>控制台</small>
        </h1>
        <p className="sub">Colab 按需 GPU 工作台 · 模型囤云盘 · 凭证不出本机</p>
        <nav className="tabs">
          {TABS.map(([id, label]) => (
            <button key={id} className={tab === id ? "tab on" : "tab"}
                    onClick={() => setTab(id)}>{label}</button>
          ))}
        </nav>
      </header>

      {tab === "overview" && <OverviewTab />}
      {tab === "models" && <ModelsTab />}
      {tab === "deploy" && <DeployTab />}
      {tab === "chat" && <ChatTab />}
    </main>
  );
}

/** 通用轮询 hook */
export function usePoll(fn, deps, ms = 3000) {
  const [data, setData] = useState(null);
  useEffect(() => {
    let alive = true;
    const run = async () => {
      try {
        const v = await fn();
        if (alive) setData({ ok: true, v });
      } catch (e) {
        if (alive) setData({ ok: false, e: String(e.message || e) });
      }
    };
    run();
    const id = setInterval(run, ms);
    return () => { alive = false; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return data;
}
