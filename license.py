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
    """امضا و ساختار توکن را بررسی می‌کند و payload را برمی‌گرداند. خطا → LicenseError."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not PUBLIC_KEY_PEM:
        raise LicenseError("کلید عمومی لایسنس تنظیم نشده است.")
    tok = (token or "").strip()
    if not tok.startswith(TOKEN_PREFIX):
        raise LicenseError("قالب کد لایسنس معتبر نیست.")
    body = tok[len(TOKEN_PREFIX):]
    if "." not in body:
        raise LicenseError("قالب کد لایسنس معتبر نیست.")
    payload_b64, sig_b64 = body.rsplit(".", 1)
    try:
        payload_raw = _b64d(payload_b64)
        sig = _b64d(sig_b64)
        payload = json.loads(payload_raw.decode("utf-8"))
    except Exception:
        raise LicenseError("کد لایسنس خراب است.")
    if not isinstance(payload, dict) or payload.get("p") != PRODUCT_ID:
        raise LicenseError("این کد متعلق به این نرم‌افزار نیست.")
    if not payload.get("u") or not payload.get("exp"):
        raise LicenseError("کد لایسنس ناقص است.")
    try:
        pub = serialization.load_pem_public_key(PUBLIC_KEY_PEM)
        assert isinstance(pub, Ed25519PublicKey)
        pub.verify(sig, payload_raw)
    except InvalidSignature:
        raise LicenseError("امضای کد لایسنس معتبر نیست (جعلی یا دست‌کاری‌شده).")
    except LicenseError:
        raise
    except Exception:
        raise LicenseError("بررسی امضا ممکن نشد.")
    return payload


def days_left(payload: dict, now: float | None = None) -> int:
    """روزهای باقی‌مانده (می‌تواند منفی باشد)."""
    try:
        exp = datetime.strptime(str(payload.get("exp")), "%Y-%m-%d").date()
        today = datetime.fromtimestamp(now if now is not None else time.time(), tz=TEHRAN).date()
        return (exp - today).days
    except Exception:
        return -10**6


def check_active(payload: dict, now: float | None = None) -> tuple[bool, str, int]:
    """(فعال؟، پیام فارسی، روزهای باقی‌مانده)."""
    left = days_left(payload, now)
    if left < 0:
        return False, "مدت لایسنس شما به پایان رسیده است. برای تمدید با فروشنده در تماس باشید.", left
    if left == 0:
        return True, "امروز آخرین روز لایسنس شماست.", left
    if left <= 7:
        return True, f"هشدار: فقط {left} روز از لایسنس شما باقی مانده است.", left
    return True, f"لایسنس معتبر است ({left} روز باقی‌مانده).", left


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
