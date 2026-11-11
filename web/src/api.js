// ModelDock API 客户端：统一错误处理
const j = (r) => {
  if (!r.ok) return r.json().then((e) => Promise.reject(new Error(e.detail || r.statusText)));
  return r.status === 204 ? null : r.json();
};

export const api = {
  // health / accounts
  health: () => fetch("/api/health").then(j),
  secrets: () => fetch("/api/accounts/secrets").then(j),
  driveStatus: () => fetch("/api/accounts/drive/status").then(j),
  colabUsage: () => fetch("/api/colab/usage").then(j),
  putSecret: (body) => fetch("/api/accounts/secrets", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) }).then(j),
  driveImport: () => fetch("/api/accounts/drive/import", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: "{}" }).then(j),
  driveOauthStart: (body) => fetch("/api/accounts/drive/oauth/start", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) }).then(j),

  // colab login (headless OAuth relay)
  colabAuthStart: () => fetch("/api/colab/auth/start", { method: "POST" }).then(j),
  colabAuthCancel: () => fetch("/api/colab/auth/cancel", { method: "POST" }).then(j),
  colabAuthSubmit: (code) => fetch("/api/colab/auth/submit", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }) }).then(j),

  // models
  families: () => fetch("/api/models/families").then(j),
  createFamily: (body) => fetch("/api/models/families", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) }).then(j),
  tasks: () => fetch("/api/models/tasks").then(j),
  createTask: (body) => fetch("/api/models/tasks", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) }).then(j),

  // recipes / deployments
  recipes: () => fetch("/api/recipes").then(j),
  deployments: () => fetch("/api/deployments").then(j),
  createDeployment: (body) => fetch("/api/deployments", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) }).then(j),
  deployment: (id) => fetch(`/api/deployments/${id}`).then(j),
  deploymentLogs: (id) => fetch(`/api/deployments/${id}/logs`).then((r) => r.text()),
  startDeployment: (id) => fetch(`/api/deployments/${id}/start`, { method: "POST" }).then(j),
  cancelDeployment: (id) => fetch(`/api/deployments/${id}/cancel`, { method: "POST" }).then(j),
  releaseDeployment: (id) => fetch(`/api/deployments/${id}/release`, { method: "POST" }).then(j),
  notebook: (id) => fetch(`/api/deployments/${id}/notebook`).then((r) => r.text()),
  tunnelStart: (id, service_port) =>
    fetch(`/api/deployments/${id}/tunnel/start`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ service_port: service_port || 0 }) }).then(j),
  tunnelStop: (id) => fetch(`/api/deployments/${id}/tunnel/stop`, { method: "POST" }).then(j),
  tunnelStatus: (id) => fetch(`/api/deployments/${id}/tunnel/status`).then(j),

  // chat
  thread: (id) => fetch(`/api/chat/threads/${id}`).then(j),
  threads: (depId) => fetch(`/api/chat/deployments/${depId}/threads`).then(j),
  createThread: (depId, body) => fetch(`/api/chat/deployments/${depId}/threads`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ deployment_id: depId, ...body }) }).then(j),
  deleteThread: (id) => fetch(`/api/chat/threads/${id}`, { method: "DELETE" }).then(j),
  setChatEndpoint: (depId, endpoint) =>
    fetch(`/api/chat/deployments/${depId}/endpoint`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint }) }).then(j),
  sendMessage: (threadId, message) =>
    fetch(`/api/chat/threads/${threadId}/messages`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }) }).then(j),

  // SSE 流式：POST + 读流；onDelta(kind, obj) 回调；返回 [DONE] 前结束
  sendMessageStream: async (threadId, message, onDelta) => {
    const resp = await fetch(`/api/chat/threads/${threadId}/messages/stream`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }) });
    if (!resp.ok || !resp.body) {
      let detail = `${resp.status}`;
      try { detail = (await resp.json()).detail || detail; } catch { /* keep */ }
      throw new Error(detail);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop() || "";
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (data === "[DONE]") return;
        try { onDelta(JSON.parse(data)); } catch { /* 忽略坏帧 */ }
      }
    }
  },
};
