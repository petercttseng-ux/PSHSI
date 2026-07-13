"""
NASA Earthdata credential management (shared by GUI and Web versions).

Token lookup order:
  1. Environment variable  NASA_EARTHDATA_TOKEN
  2. Project file          <project root>/earthdata_token.txt
  3. User file             ~/.earthdata_token

No credentials are hard-coded in source. Obtain / renew a token at:
  https://urs.earthdata.nasa.gov/users/<username>/user_tokens

If no token is found, requests falls back to ~/.netrc (machine
urs.earthdata.nasa.gov) automatically — create that file yourself if you
prefer username/password authentication.
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import pathlib
from typing import Optional

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
TOKEN_FILE_CANDIDATES = [
    PROJECT_ROOT / "earthdata_token.txt",
    pathlib.Path.home() / ".earthdata_token",
]
ENV_VAR = "NASA_EARTHDATA_TOKEN"


# ── OS trust store ─────────────────────────────────────────────────────────
# 機關網路常以自有憑證做 TLS 攔截，Python 預設的 certifi 不信任它。
# truststore 讓 Python 改用 Windows/macOS 系統憑證庫（與 Chrome 相同），
# 在不關閉憑證驗證的前提下解決 CERTIFICATE_VERIFY_FAILED。
def _enable_os_truststore() -> bool:
    try:
        import truststore
        truststore.inject_into_ssl()
        return True
    except Exception:
        return False


TRUSTSTORE_ACTIVE = _enable_os_truststore()


def get_token() -> Optional[str]:
    """Return the Earthdata bearer token, or None if not configured."""
    tok = os.environ.get(ENV_VAR, "").strip()
    if tok:
        return tok
    for p in TOKEN_FILE_CANDIDATES:
        try:
            if p.exists():
                # take the first non-empty, non-comment line
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line
        except Exception:
            continue
    return None


def _decode_jwt_payload(token: str) -> dict:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


def token_status() -> dict:
    """
    Return {"present": bool, "valid": bool, "uid": str|None,
            "expires": iso str|None, "days_left": float|None, "message": str}.
    """
    tok = get_token()
    if not tok:
        return {
            "present": False, "valid": False, "uid": None,
            "expires": None, "days_left": None,
            "message": (
                f"未設定 NASA Earthdata token。請設定環境變數 {ENV_VAR}，"
                f"或將 token 存於 {TOKEN_FILE_CANDIDATES[0].name}（專案根目錄）。"
            ),
        }
    payload = _decode_jwt_payload(tok)
    exp = payload.get("exp")
    uid = payload.get("uid")
    if not exp:
        # Not a JWT we can decode — assume usable, let the server decide.
        return {
            "present": True, "valid": True, "uid": uid,
            "expires": None, "days_left": None,
            "message": "Token 已設定（無法解析到期日，將由伺服器驗證）。",
        }
    exp_dt = datetime.datetime.fromtimestamp(exp, tz=datetime.timezone.utc)
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    days_left = (exp_dt - now).total_seconds() / 86400
    if days_left <= 0:
        msg = (
            f"Token 已於 {exp_dt:%Y-%m-%d} 過期！請至 "
            f"https://urs.earthdata.nasa.gov/users/{uid or '<user>'}/user_tokens 重新產生。"
        )
        valid = False
    elif days_left < 14:
        msg = f"Token 將於 {exp_dt:%Y-%m-%d} 到期（剩 {days_left:.0f} 天），請儘早更新。"
        valid = True
    else:
        msg = f"Token 有效，至 {exp_dt:%Y-%m-%d}。"
        valid = True
    return {
        "present": True, "valid": valid, "uid": uid,
        "expires": exp_dt.isoformat(), "days_left": round(days_left, 1),
        "message": msg,
    }


def auth_session():
    """
    Return a requests.Session for Earthdata.

    - Bearer token header if a token is configured.
    - Otherwise no explicit auth: requests will use ~/.netrc automatically.
    - TLS verification is ON, using the OS trust store when the
      `truststore` package is installed (pip install truststore).
    """
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": "GHRSST-MUR-Viewer/2.0 (FRI MOA)"})
    tok = get_token()
    if tok:
        s.headers.update({"Authorization": f"Bearer {tok}"})
    return s
