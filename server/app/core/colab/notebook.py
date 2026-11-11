"""手动兜底：为一次部署生成可粘贴到 Colab 的 Notebook 单元格。

背景（DESIGN.md §9 风险表）：Colab 非公开 API 变动会让自动编排失效；
此单元格覆盖同一套步骤（install → Drive 拉模型 → 启动），可在网页 Colab
手工执行。单元格不含任何凭证；临时 rclone 配置由用户运行时粘贴
（提示来自本机托管配置 `~/.modeldock/rclone.conf` 的 [gdrive] 段）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.colab.orchestrator import MODELS_DIR
from app.db import Deployment, ModelFamily, ModelFile, Recipe, get_db_engine

PASTE_HERE = 'PASTE_RCLONE_CONFIG_HERE'


def render_cell(dep_id: int) -> str:
    with Session(get_db_engine()) as s:
        dep = s.get(Deployment, dep_id)
        if dep is None:
            raise ValueError(f"deployment {dep_id} not found")
        recipe = s.get(Recipe, dep.recipe_id)
        if recipe is None:
            raise ValueError(f"recipe {dep.recipe_id} not found")
        manifest = recipe.manifest or {}
        model_lines: list[str] = []
        for role, spec in (manifest.get("models") or {}).items():
            fam = s.scalars(select(ModelFamily)
                            .where(ModelFamily.name == spec.get("family"))).first()
            if not fam:
                raise ValueError(f"model family '{spec.get('family')}' not found (role {role})")
            for mf in s.scalars(select(ModelFile)
                                .where(ModelFile.family_id == fam.id)).all():
                model_lines.append(
                    f"    (\"{fam.name}\", \"{mf.remote_path}\"),"
                )
        gpu = dep.gpu or "T4"
        run_script = recipe.run_script or "echo 'no run script'"
        install_script = recipe.install_script or ""
        ports = list(recipe.ports or [])

    models_literal = "\n".join(model_lines) or "    # (manifest has no models)"
    return f'''# —— ModelDock 手动部署单元格（deployment #{dep_id}: {recipe.name}）——
# 推荐运行时：GPU={gpu}（Colab 菜单 代码执行程序 → 更改运行时类型）
# 1) 安装 rclone（若配方有额外安装脚本，替换 INSTALL_SCRIPT）
import subprocess, textwrap

def sh(c, **kw):
    r = subprocess.run(["bash", "-c", c], capture_output=True, text=True, **kw)
    print(r.stdout, r.stderr)
    assert r.returncode == 0, f"command failed rc={{r.returncode}}"

sh("command -v rclone >/dev/null || (curl -s https://rclone.org/install.sh | bash)")

INSTALL_SCRIPT = {install_script!r}
if INSTALL_SCRIPT:
    sh(INSTALL_SCRIPT)

# 2) 临时 Drive 通道：把本机 ~/.modeldock/rclone.conf 的 [gdrive] 段粘贴到下面
RCLONE_CONF = {PASTE_HERE!r}
assert RCLONE_CONF != {PASTE_HERE!r}, "请先把托管 rclone 配置粘贴进来（用完即删）"
open("/content/.secrets/rclone.conf", "w").write(RCLONE_CONF)
subprocess.run(["bash", "-c", "mkdir -p /content/.secrets {MODELS_DIR} && "
               "chmod 700 /content/.secrets && "
               "chmod 600 /content/.secrets/rclone.conf"])

# 3) 从 Drive 拉模型（与自动编排相同路径）
MODELS = [
{models_literal}
]
for fam, rel in MODELS:
    print(f"[sync] {{fam}}/{{rel}}")
    sh(f"rclone copy --config /content/.secrets/rclone.conf "
       f"gdrive:{settings.drive_prefix}/{{rel}} {MODELS_DIR}/{{fam}}/ --progress")

# 4) 启动服务（后台运行；日志 /content/mdx_run.log）
sh("nohup bash -c " + {run_script!r} + " > /content/mdx_run.log 2>&1 & echo started")
print("服务端口声明: {ports or '无'}; 就绪后查看 /content/mdx_run.log")

# 5) 用完释放：删除临时凭证（会话结束后 VM 自动回收）
# sh("rm -f /content/.secrets/rclone.conf; rm -rf {MODELS_DIR}")
'''


def config_hint() -> str:
    """提示用户去哪个本机文件取 [gdrive] 段（不含值本身）。"""
    return str(settings.rclone_config_path)
