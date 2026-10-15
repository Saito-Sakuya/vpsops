"""Telegram 回调鉴权与日志脱敏。"""

import pytest

from vpsops.gateway import config as gwconfig
from vpsops.gateway.telegram import Telegram

TOKEN = "123456:ABCdefGHIjklMNOpqrSTUvwxYZ0123456789"


def cq(chat=100, user=100, mid=7, data="grant:j_1:1800"):
    return {"id": "q", "data": data, "from": {"id": user}, "message": {"message_id": mid, "chat": {"id": chat}}}


def test_private_chat_defaults_approver_to_chat_id():
    tg = Telegram(TOKEN, 100)
    assert tg.enabled and tg.approvers == {100}
    assert tg.authorize(cq())[0]
    assert not tg.authorize(cq(chat=999))[0]
    assert not tg.authorize(cq(user=555))[0]


def test_group_requires_explicit_approvers():
    assert not Telegram(TOKEN, -100123).enabled  # 群聊且没有 approvers：禁用，而不是放行所有人
    tg = Telegram(TOKEN, -100123, {42})
    assert tg.authorize(cq(chat=-100123, user=42))[0]
    ok, why = tg.authorize(cq(chat=-100123, user=43))
    assert not ok and "approvers" in why


def test_missing_message_rejected():
    tg = Telegram(TOKEN, 100)
    bad = cq()
    del bad["message"]["message_id"]
    assert not tg.authorize(bad)[0]


def test_token_scrubbed_from_errors():
    tg = Telegram(TOKEN, 100)
    msg = tg._scrub(f"Cannot connect to https://api.telegram.org/bot{TOKEN}/getUpdates")
    assert TOKEN not in msg and "<bot-token>" in msg
    assert "ABCdef" not in Telegram(None, None)._scrub(f"x bot{TOKEN} y")


def test_config_rejects_group_without_approvers(tmp_path):
    tok = tmp_path / "tg.token"
    tok.write_text(TOKEN)
    base = (f"listen: 127.0.0.1:1\nstate_dir: {tmp_path.as_posix()}\naudit_dir: {tmp_path.as_posix()}\n"
            f"telegram:\n  bot_token_file: {tok.as_posix()}\n")
    p = tmp_path / "g.yaml"
    p.write_text(base + "  chat_id: -100123\n")
    with pytest.raises(ValueError, match="approvers"):
        gwconfig.load(p)
    p.write_text(base + "  chat_id: -100123\n  approvers: [42]\n")
    assert gwconfig.load(p).tg_approvers == {42}
