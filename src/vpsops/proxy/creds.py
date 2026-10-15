"""token 存取：Windows 凭据管理器 / macOS 钥匙串 / Secret Service（通过 keyring）。"""

import os

SERVICE = "vpsops"


def _keyring():
    import keyring
    return keyring


def get_token(client: str) -> str | None:
    env = os.environ.get("VPSOPS_TOKEN")  # 仅用于测试
    if env:
        return env
    return _keyring().get_password(SERVICE, client)


def set_token(client: str, token: str):
    _keyring().set_password(SERVICE, client, token)


def get_url() -> str | None:
    return os.environ.get("VPSOPS_URL") or _keyring().get_password(SERVICE, "_gateway_url")


def set_url(url: str):
    _keyring().set_password(SERVICE, "_gateway_url", url)


def get_ca() -> str | None:
    return os.environ.get("VPSOPS_CA") or _keyring().get_password(SERVICE, "_gateway_ca")


def set_ca(path: str):
    _keyring().set_password(SERVICE, "_gateway_ca", path)
