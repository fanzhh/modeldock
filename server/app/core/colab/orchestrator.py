"""部署编排器：queued→provision→install→syncing_models→booting→ready，
release: releasing→done；任一步 failed/cancelled。可从最近成功步重试（幂等步定义）。

设计（DESIGN.md §4.3）：
1. provision：请求 GPU，A100→L4→T4 按链降级
2. install：配方 install 脚本；模型装配需要 rclone 时自动补装
3. syncing_models：临时 rclone 配置上传 /content/.secrets（0600 幂等），
   逐文件 Drive→Colab 本地复制
4. booting：run 脚本后台启动 + 端口健康检查
5. ready：记录访问信息；release：删临时凭证 → 停会话
"""
from __future__ import annotations

import asyncio
import configparser
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.colab.base import GPU_FALLBACK, ColabBackend, ColabError
from app.core.drive import Drive
from app.db import Deployment, ModelFamily, ModelFile, Recipe, get_db_engine, utcnow

log = logging.getLogger("modeldock.orchestrator")

STEP_ORDER = ["queued", "provision", "install", "syncing_models", "booting", "ready",
              "releasing", "done"]

SECRETS_REMOTE_PATH = "/content/.secrets/rclone.conf"
MODELS_DIR = "/content/models"


@dataclass
class _RunState:
    deployment_id: int
    cancel: asyncio.Event = asyncio.Event()


class Orchestrator:
    def __init__(self, backend: ColabBackend, drive: Drive | None = None) -> None:
        self.backend = backend
        self.drive = drive or Drive()
        self._running: dict[int, _RunState] = {}

    # ---- lifecycle ----
    def start(self, dep_id: int) -> bool:
        """启动（或继续）一个部署。已在跑 → False。"""
        if dep_id in self._running:
            return False
        st = _RunState(deployment_id=dep_id)
        self._running[dep_id] = st
        asyncio.get_running_loop().create_task(self._pipeline(st))
        return True

    def cancel(self, dep_id: int) -> bool:
        st = self._running.get(dep_id)
        if not st:
            return False
        st.cancel.set()
        return True

    def busy(self) -> dict[int, str]:
        return {i: "running" for i in self._running}

    # ---- DB helpers ----
    @staticmethod
    def _set_state(dep_id: int, state: str, *, logs_append: str = "") -> None:
        with Session(get_db_engine()) as s:
            d = s.get(Deployment, dep_id)
            if d is None:
                return
            d.state = state
            hist = list(d.state_history or [])
            hist.append({"state": state, "at": utcnow().isoformat()})
            d.state_history = hist
            if logs_append:
                d.logs = (d.logs or "") + logs_append
            s.commit()

    @staticmethod
    def _append_log(dep_id: int, text: str) -> None:
        with Session(get_db_engine()) as s:
            d = s.get(Deployment, dep_id)
            if d is None:
                return
            d.logs = (d.logs or "") + text
            s.commit()

    @staticmethod
    def _set_params(dep_id: int, **kv) -> None:
        with Session(get_db_engine()) as s:
            d = s.get(Deployment, dep_id)
            if d is None:
                return
            params = dict(d.params or {})
            params.update(kv)
            d.params = params
            s.commit()

    # ---- artifact helpers ----
    def _recipe(self, dep_id: int) -> tuple[Recipe, Deployment]:
        with Session(get_db_engine()) as s:
            d = s.get(Deployment, dep_id)
            if d is None:
                raise ColabError(f"deployment {dep_id} not found")
            r = s.get(Recipe, d.recipe_id)
            return r, d

    def _model_files(self, manifest: dict, params: dict | None = None) -> list[tuple[str, str, int]]:
        """manifest.models 中每个 role 的 (family, file_remote_rel, bytes)。

        family 模板："$<key>" → Deployment.params.families[key]（内置配方用）。
        """
        families = (params or {}).get("families") or {}
        out: list[tuple[str, str, int]] = []
        with Session(get_db_engine()) as s:
            for role, spec in (manifest.get("models") or {}).items():
                fam_name = spec.get("family", "")
                if fam_name.startswith("$"):
                    key = fam_name[1:]
                    fam_name = families.get(key)
                    if not fam_name:
                        raise ColabError(
                            f"family template '{spec.get('family')}' not resolved: "
                            f"pass params.families.{key} when creating the deployment")
                fam = s.scalars(select(ModelFamily).where(ModelFamily.name == fam_name)).first()
                if not fam:
                    raise ColabError(f"model family '{fam_name}' not found in repo (role {role})")
                files = s.scalars(select(ModelFile).where(ModelFile.family_id == fam.id)).all()
                if not files:
                    raise ColabError(f"family '{fam_name}' has no model files registered")
                for mf in files:
                    out.append((fam.name, mf.remote_path, mf.bytes or 0))
        return out

    def _dump_rclone_conf(self) -> str:
        """托管 rclone 配置全文 → 注入 Colab 的临时副本。

        TODO(最小权限)：Drive scope 换只读后此处只输出只读段；
        当前为纯托管配置（单 remote gdrive），随会话销毁。
        """
        parser = configparser.ConfigParser()
        parser.read(str(self.drive.config_path))
        buf: list[str] = []
        for sec in parser.sections():
            buf.append(f"[{sec}]")
            for k, v in parser.items(sec):
                buf.append(f"{k} = {v}")
        return "\n".join(buf) + "\n"

    # ---- pipeline ----
    async def _pipeline(self, st: _RunState) -> None:
        dep_id = st.deployment_id
        try:
            recipe, dep = self._recipe(dep_id)
            manifest = recipe.manifest or {}
            models = self._model_files(manifest, dep.params or {})  # 预检，缺模型直接失败
            if models and not self.drive.has_remote():
                raise ColabError(
                    "drive remote not registered; run accounts drive import/oauth first")
            gpu = dep.gpu or "T4"

            # 1. provision（带降级链）；启动前抓余额快照（DESIGN.md §4.5）
            usage_before = await asyncio.to_thread(self.backend.usage)
            if usage_before:
                self._set_params(dep_id, usage_before=usage_before)
            self._set_state(dep_id, "provision", logs_append="[provision] creating session…\n")
            session = await self._provision(st, gpu)
            self._set_params(dep_id, session={"id": session.session_id, "gpu": session.gpu})

            # 2. install
            self._set_state(dep_id, "install")
            install_bash = self._install_script(recipe, bool(models))
            await self._exec_step(st, dep_id, session.session_id, "install", install_bash,
                                  timeout=1800)

            # 3. syncing_models
            if models:
                self._set_state(dep_id, "syncing_models")
                self.backend.upload_text(session.session_id, SECRETS_REMOTE_PATH,
                                         self._dump_rclone_conf())
                sync_bash = self._sync_script(models)
                await self._exec_step(st, dep_id, session.session_id, "syncing_models",
                                      sync_bash, timeout=43_200, log_rate_limit=10.0)
            else:
                self._append_log(dep_id, "[syncing_models] manifest has no models; skip\n")

            # 4. booting（后台启动 + 端口健康检查）
            self._set_state(dep_id, "booting")
            ports = list(recipe.ports or manifest.get("ports") or [])
            boot_bash = self._boot_script(recipe, ports)
            await self._exec_step(st, dep_id, session.session_id, "booting", boot_bash,
                                  timeout=900)

            self._set_state(dep_id, "ready", logs_append="[ready] deployment ready\n")
            self._set_params(dep_id, access={
                "note": "tunnel via `colab ssh -s <session>`; 服务端口 " +
                        ", ".join(str(p) for p in ports) if ports else "no ports declared",
                "ports": ports,
                "session": session.session_id,
            })
        except asyncio.CancelledError:
            self._set_state(dep_id, "cancelled", logs_append="[cancelled]\n")
        except Exception as e:  # noqa: BLE001
            log.exception("deployment %s failed", dep_id)
            self._set_state(dep_id, "failed", logs_append=f"[failed] {str(e)[:400]}\n")
        finally:
            self._running.pop(dep_id, None)

    async def _provision(self, st: _RunState, gpu: str) -> object:
        chain = GPU_FALLBACK.get(gpu, [gpu])
        last_err = ""
        for cand in chain:
            if st.cancel.is_set():
                raise asyncio.CancelledError
            self._append_log(st.deployment_id, f"[provision] try gpu={cand}\n")
            try:
                session = await asyncio.to_thread(self.backend.create_session, cand)
                self._append_log(st.deployment_id,
                                 f"[provision] session {session.session_id} on {session.gpu}\n")
                if session.gpu != cand:
                    self._append_log(st.deployment_id,
                                     f"[provision] downgraded to {session.gpu}\n")
                return session
            except ColabError as e:
                last_err = str(e)
                self._append_log(st.deployment_id, f"[provision] {cand} unavailable: {last_err}\n")
        raise ColabError(f"no GPU available (tried {'→'.join(chain)}): {last_err}")

    async def _exec_step(self, st: _RunState, dep_id: int, session_id: str, step: str,
                         bash: str, timeout: float, log_rate_limit: float = 0.0) -> None:
        if st.cancel.is_set():
            raise asyncio.CancelledError
        self._append_log(dep_id, f"[{step}] exec…\n")
        out = await asyncio.to_thread(self.backend.exec_script, session_id, bash, timeout)
        if st.cancel.is_set():
            raise asyncio.CancelledError
        if not out.ok:
            raise ColabError(f"step {step} failed: {(out.detail or out.stdout)[-300:]}")
        if log_rate_limit:
            self._append_log(dep_id, f"[{step}] ok ({out.stdout[-200:]})\n")
        else:
            self._append_log(dep_id, f"[{step}] ok\n")

    # ---- 脚本生成 ----
    @staticmethod
    def _install_script(recipe: Recipe, need_rclone: bool) -> str:
        parts = ["set -e"]
        if need_rclone:
            parts.append("command -v rclone >/dev/null 2>&1 || "
                         "(curl -s https://rclone.org/install.sh | bash)")
        if recipe.install_script:
            parts.append(recipe.install_script)
        return "\n".join(parts) + "\n"

    @staticmethod
    def _sync_script(models: list[tuple[str, str, int]]) -> str:
        lines = ["set -e", f"mkdir -p {MODELS_DIR}", 'chmod 700 /content/.secrets 2>/dev/null || true',
                 f"chmod 600 {SECRETS_REMOTE_PATH}"]
        total = sum(b for _, _, b in models) or 0
        for i, (fam, rel, _b) in enumerate(models):
            fname = rel.rsplit("/", 1)[-1]
            lines.append(
                f"echo '[sync {i + 1}/{len(models)}] {fam}/{fname}'\n"
                f"rclone copy --config {SECRETS_REMOTE_PATH} "
                f"gdrive:{settings.drive_prefix}/{rel} {MODELS_DIR}/{fam}/ --progress"
            )
        lines.append(f"echo '[sync] all {len(models)} files done (~{total >> 20} MiB)'")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _boot_script(recipe: Recipe, ports: list[int]) -> str:
        lines = ["set -e"]
        if recipe.run_script:
            lines.append(f"(nohup bash -c {recipe.run_script!r} "
                         "> /content/mdx_run.log 2>&1 &) ")
            lines.append("echo '[booting] run script started; log: /content/mdx_run.log'")
        else:
            lines.append("echo '[booting] recipe has no run script'")
        for port in ports:
            # 健康检查：最多 30×5s；2xx/3xx 即就绪，超时只告警（服务可能仍冷启动）
            lines.append(
                "ok=0\n"
                "for i in $(seq 1 30); do\n"
                f"  code=$(curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{port} || true)\n"
                "  case $code in 2*|3*) ok=1; break;; esac\n"
                "  sleep 5\n"
                "done\n"
                f"if [ $ok = 1 ]; then echo '[health] port {port} ready'; "
                f"else echo '[health] WARN port {port} not responding after 150s'; fi"
            )
        return "\n".join(lines) + "\n"

    # ---- release ----
    async def release(self, dep_id: int) -> str:
        """释放会话：删临时凭证 → 停 VM → releasing→done。幂等。"""
        recipe, dep = self._recipe(dep_id)
        params = dep.params or {}
        session = (params.get("session") or {}).get("id")
        self._set_state(dep_id, "releasing", logs_append="[releasing] cleanup…\n")
        if session:
            try:
                await asyncio.to_thread(
                    self.backend.exec_script, session,
                    f"rm -f {SECRETS_REMOTE_PATH}; rm -rf {MODELS_DIR}", 60)
            except Exception as e:  # noqa: BLE001
                self._append_log(dep_id, f"[releasing] secret cleanup warn: {e}\n")
            await asyncio.to_thread(self.backend.stop_session, session)
        # 释放后余额快照（会话时长×GPU≈points 消耗估算依据）
        usage_after = await asyncio.to_thread(self.backend.usage)
        if usage_after:
            self._set_params(dep_id, usage_after=usage_after)
        self._set_state(dep_id, "done", logs_append="[done] session released\n")
        return "done"


orchestrator_singleton: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global orchestrator_singleton
    if orchestrator_singleton is None:
        from app.core.colab.cli import backend_from_env
        orchestrator_singleton = Orchestrator(backend_from_env())
    return orchestrator_singleton


def set_orchestrator(o: Orchestrator) -> None:
    """测试注入。"""
    global orchestrator_singleton
    orchestrator_singleton = o
