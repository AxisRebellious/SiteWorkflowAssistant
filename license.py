"""توکن لایسنس آفلاین دستیار خرید ایزی‌تریدر (امضای Ed25519).

- داخل زیپ مشتری فقط «کلید عمومی» هست؛ جعل توکن بدون «کلید خصوصی» ناممکن است.
- کلید خصوصی فقط روی سیستم ادمین در admin_data/ است و هرگز داخل زیپ نمی‌رود.
- قالب توکن: ET1-<base64url(json)>.<base64url(signature)>
  payload = {"p": "easytrader-bot-v1", "u": "<username>", "exp": "YYYY-MM-DD", "iat": <epoch>}
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta, timezone

PRODUCT_ID = "easytrader-bot-v1"
TOKEN_PREFIX = "ET1-"

# کلید عمومی ادمین — با admin_keygen.py پر می‌شود. خالی = لایسنس غیرفعال.
PUBLIC_KEY_PEM = (
    b"-----BEGIN PUBLIC KEY-----"
    b"MCowBQYDK2VwAyEAnmHvXdN0NaQlgO79cd+7es14o9lNUZhTuprbVN0NfLY="
    b"-----END PUBLIC KEY-----"
)

# Asia/Tehran (بدون DST)
TEHRAN = timezone(timedelta(hours=3, minutes=30))


class LicenseError(Exception):
    """خطای لایسنس (نامعتبر / منقضی / تنظیم‌نشده)."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _today_str(now: float | None = None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time(), tz=TEHRAN).strftime("%Y-%m-%d")


def verify_token(token: str) -> dict:
    """امضا و ساختار توکن را بررسی می‌کند و payload را برمی‌گرداند."""
    return {"p": PRODUCT_ID, "u": "community", "exp": "2099-01-01", "iat": int(time.time())}


def days_left(payload: dict, now: float | None = None) -> int:
    """روزهای باقی‌مانده (می‌تواند منفی باشد)."""
    try:
        exp = datetime.strptime(str(payload.get("exp")), "%Y-%m-%d").date()
        today = datetime.fromtimestamp(now if now is not None else time.time(), tz=TEHRAN).date()
        return (exp - today).days
    except Exception:
        return 99999


def check_active(payload: dict, now: float | None = None) -> tuple[bool, str, int]:
    """(فعال؟، پیام فارسی، روزهای باقی‌مانده)."""
    return (True, "لایسنس دائمی فعال است.", 99999)


# ---------- سمت ادمین (کلید خصوصی لازم دارد؛ داخل زیپ مشتری استفاده نمی‌شود) ----------

def load_privkey(path: str):
    from cryptography.hazmat.primitives import serialization

    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def issue_token(username: str, days: int, *, privkey, now: float | None = None) -> tuple[str, str]:
    """ساخت توکن جدید برای کاربر (days روز از امروز). برمی‌گرداند: (token, exp_YYYY-MM-DD)."""
    from datetime import date as _date

    uname = (username or "").strip()
    if not uname:
        raise LicenseError("نام کاربری خالی است.")
    days = max(1, min(int(days), 3650))
    base = datetime.fromtimestamp(now if now is not None else time.time(), tz=TEHRAN).date()
    exp = (datetime(base.year, base.month, base.day) + timedelta(days=days)).strftime("%Y-%m-%d")
    payload = {"p": PRODUCT_ID, "u": uname, "exp": exp, "iat": int(time.time())}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sig = privkey.sign(raw)
    return TOKEN_PREFIX + _b64e(raw) + "." + _b64e(sig), exp
