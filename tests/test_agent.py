import json

from conftest import ai_agent, script


def test_unknown_op_and_source(agent_env):
    call = agent_env["call"]
    assert call("rm")["error"] == "unknown_op"
    r = call("logs", source="/etc/shadow")
    assert r["error"] == "unknown_source" and r["available"] == ["app"]


def test_logs_tail_and_grep(agent_env):
    call = agent_env["call"]
    r = call("logs", source="app", lines=3)["data"]
    assert r["count"] == 3 and r["text"].splitlines()[-1].startswith("line 500")
    r = call("logs", source="app", lines=1000, grep="ERROR")["data"]
    assert r["count"] == 50
    assert call("logs", source="app", grep="(")["error"] == "bad_args"


def test_config_read_redacts_secret_env(agent_env):
    r = agent_env["call"]("config_read", name="env")["data"]
    assert "hunter2" not in r["content"]
    assert "DB_PASSWORD=[REDACTED len=14]" in r["content"]
    assert "APP_NAME=demo" in r["content"] and r["redacted"]


def test_config_edit_ok(agent_env):
    call = agent_env["call"]
    sha = call("config_read", name="app")["data"]["sha256"]
    r = call("config_edit", name="app", sha256=sha, edits=[{"old": "workers = 4", "new": "workers = 8"}])
    assert r["ok"], r
    assert r["data"]["changed"] and "+workers = 8" in r["data"]["diff"]
    assert agent_env["conf"].read_text() == "workers = 8\nlisten = 80\n"
    assert agent_env["applied"].read_text().count("applied") == 1
    assert len(call("config_read", name="app")["data"]["backups"]) == 1


def test_config_edit_sha_mismatch_and_ambiguous(agent_env):
    call = agent_env["call"]
    r = call("config_edit", name="app", sha256="x", edits=[{"old": "workers", "new": "w"}])
    assert r["error"] == "sha_mismatch"
    sha = call("config_read", name="app")["data"]["sha256"]
    r = call("config_edit", name="app", sha256=sha, edits=[{"old": " = ", "new": "="}])
    assert r["error"] == "old_not_unique"
    r = call("config_edit", name="app", sha256=sha, edits=[{"old": "nope", "new": "x"}])
    assert r["error"] == "old_not_found"


def test_config_edit_validate_fail_leaves_file(agent_env):
    call = agent_env["call"]
    before = agent_env["conf"].read_text()
    sha = call("config_read", name="app")["data"]["sha256"]
    r = call("config_edit", name="app", sha256=sha, edits=[{"old": "workers = 4", "new": "BROKEN"}])
    assert r["error"] == "validate_failed"
    assert agent_env["conf"].read_text() == before
    assert not agent_env["applied"].exists()


def test_config_edit_apply_fail_rolls_back(agent_env):
    cfg = agent_env["cfg"]
    cfg["configs"]["app"]["apply"] = script(agent_env["tmp"], "bad_apply", "import sys\nsys.exit(3)\n")
    agent_env["cfg_path"].write_text(json.dumps(cfg))
    call = agent_env["call"]
    before = agent_env["conf"].read_text()
    sha = call("config_read", name="app")["data"]["sha256"]
    r = call("config_edit", name="app", sha256=sha, edits=[{"old": "workers = 4", "new": "workers = 9"}])
    assert r["error"] == "apply_failed" and r["rolled_back"]
    assert agent_env["conf"].read_text() == before


def test_secret_lines_are_immutable_but_others_editable(agent_env):
    call = agent_env["call"]
    r = call("config_read", name="env")["data"]
    sha = r["sha256"]
    bad = call("config_edit", name="env", sha256=sha,
               edits=[{"old": "DB_PASSWORD=[REDACTED len=14]", "new": "DB_PASSWORD=x"}])
    assert bad["error"] == "redacted_line"
    ok = call("config_edit", name="env", sha256=sha, edits=[{"old": "PORT=8080", "new": "PORT=9090\nNEW_KEY=1"}])
    assert ok["ok"], ok
    text = agent_env["env"].read_text()
    assert "DB_PASSWORD=hunter2hunter2" in text and "PORT=9090\nNEW_KEY=1" in text


def test_config_restore(agent_env):
    call = agent_env["call"]
    sha = call("config_read", name="app")["data"]["sha256"]
    call("config_edit", name="app", sha256=sha, edits=[{"old": "workers = 4", "new": "workers = 8"}])
    r = call("config_restore", name="app")
    assert r["ok"] and r["data"]["changed"]
    assert agent_env["conf"].read_text() == "workers = 4\nlisten = 80\n"


def test_redaction_patterns():
    text = ("token=abcd1234\npostgres://u:secretpw@db/x\nkey sk-ABCDEFGHIJKLMNOPQRSTUVWX\n"
            "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\nfine\n")
    out = ai_agent.redact_lines(text)
    assert "abcd1234" not in out and "secretpw" not in out and "sk-ABC" not in out and "AAAA" not in out
    assert out.count("\n") == text.count("\n") and out.endswith("fine\n")


def test_deploy_script_rollback(agent_env, tmp_path):
    marker = tmp_path / "version.txt"
    deploy_sh = script(tmp_path, "deploy",
                       "import os,sys\nv=os.environ['VERSION']\n"
                       f"open(r'{marker}','w').write(v)\nsys.exit(1 if v=='v9.9.9' else 0)\n")
    cfg = agent_env["cfg"]
    # script 类型的 run 是单个可执行文件路径；这里用 .py 包装脚本 + 解释器不方便，改写成批处理/sh
    import sys as _sys
    wrapper = tmp_path / ("deploy.cmd" if _sys.platform == "win32" else "deploy.sh")
    if _sys.platform == "win32":
        wrapper.write_text(f"@{deploy_sh} %*\n")
    else:
        wrapper.write_text(f"#!/bin/sh\nexec {deploy_sh}\n")
        wrapper.chmod(0o755)
    cfg["deploys"] = {"app": {"type": "script", "run": str(wrapper), "version_regex": r"^v\d+\.\d+\.\d+$"}}
    agent_env["cfg_path"].write_text(json.dumps(cfg))
    call = agent_env["call"]
    assert call("deploy", target="app", version="latest; rm -rf /")["error"] == "bad_version"
    assert call("deploy", target="app", version="v1.0.0")["data"]["changed"]
    assert call("deploy", target="app", version="v1.1.0")["data"]["to"] == "v1.1.0"
    r = call("deploy", target="app", version="v9.9.9")
    assert r["error"] == "switch_failed" and r["rolled_back"]
    assert marker.read_text() == "v1.1.0"
    r = call("deploy", target="app", version="rollback")["data"]
    assert r["to"] == "v1.0.0"
    assert call("describe")["data"]["deploys"]["app"]["current"] == "v1.0.0"
