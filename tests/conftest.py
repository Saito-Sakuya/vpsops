import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ai_agent", ROOT / "agent" / "ai_agent.py")
ai_agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ai_agent)

PY = sys.executable.replace("\\", "/")


def script(tmp: Path, name: str, body: str) -> str:
    """生成一个 python 小脚本，返回可放进命令模板的调用串。"""
    p = tmp / f"{name}.py"
    p.write_text(body, encoding="utf-8")
    return f'"{PY}" "{p.as_posix()}"'


@pytest.fixture
def agent_env(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    conf = etc / "app.conf"
    conf.write_text("workers = 4\nlisten = 80\n", encoding="utf-8")
    env = etc / "app.env"
    env.write_text("APP_NAME=demo\nDB_PASSWORD=hunter2hunter2\nPORT=8080\n", encoding="utf-8")
    log = tmp_path / "app.log"
    log.write_text("".join(f"line {i} {'ERROR boom' if i % 10 == 0 else 'ok'}\n" for i in range(1, 501)),
                   encoding="utf-8")
    applied = tmp_path / "applied.txt"
    # validate：内容里出现 BROKEN 就失败
    validate = script(tmp_path, "validate",
                      "import sys\nsys.exit(1 if 'BROKEN' in open(sys.argv[1]).read() else 0)\n")
    apply = script(tmp_path, "apply",
                   f"open(r'{applied}', 'a').write('applied\\n')\n")
    cfg = {
        "state_dir": str(tmp_path / "state"),
        "logs": {"app": f"file:{log}"},
        "configs": {
            "app": {"path": str(conf), "validate": validate + " {tmp}", "apply": apply},
            "env": {"path": str(env), "secret": True, "apply": apply},
        },
        "services": {},
        "deploys": {},
    }
    cfg_path = tmp_path / "ai-agent.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    def call(op, **args):
        return ai_agent.handle({"op": op, "args": args}, str(cfg_path))

    return {"call": call, "conf": conf, "env": env, "applied": applied, "tmp": tmp_path,
            "cfg_path": cfg_path, "cfg": cfg}
