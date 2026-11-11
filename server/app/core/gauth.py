"""Google OAuth 2.0 (installed-app) 流程：授权 URL 构造 + code→token 交换。

安全约定：
- client_secret 与 refresh_token 只存 vault / rclone.conf(0600)，日志与 API 输出脱敏
- state 随机数绑定一次会话，回调校验，防 CSRF
- redirect_uri 固定为 http://127.0.0.1:{port}/api/accounts/drive/oauth/callback（仅本机）
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive"


@dataclass
class OAuthSession:
    state: str
    client_id: str
    client_secret: str
    redirect_uri: str


class GAuthError(RuntimeError):
    pass


def authorize_url(sess: OAuthSession) -> str:
    params = {
        "client_id": sess.client_id,
        "redirect_uri": sess.redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",       # 拿 refresh_token
        "prompt": "consent",
        "state": sess.state,
    }
    return str(httpx.Request("GET", AUTH_URL, params=params).url)


def new_session(client_id: str, client_secret: str, redirect_uri: str) -> OAuthSession:
    return OAuthSession(state=secrets.token_urlsafe(24), client_id=client_id,
                        client_secret=client_secret, redirect_uri=redirect_uri)


def exchange_code(sess: OAuthSession, code: str, http: httpx.Client | None = None) -> dict:
    """code → {access_token, refresh_token, expires_in, ...}。refresh_token 可能为空
    （用户此前已授权过同一 client），为空视为错误，要求 prompt=consent 重试。"""
    body = {
        "client_id": sess.client_id,
        "client_secret": sess.client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": sess.redirect_uri,
    }
    client = http or httpx.Client(timeout=20)
    try:
        r = client.post(TOKEN_URL, data=body)
        if r.status_code != 200:
            raise GAuthError(f"token exchange failed: HTTP {r.status_code}")
        data = r.json()
    finally:
        if http is None:
            client.close()
    if "refresh_token" not in data:
        raise GAuthError("Google 未返回 refresh_token（旧授权未撤销）。请撤销应用授权后重试")
    return data
