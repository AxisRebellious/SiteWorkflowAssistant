"""ربات پیام‌رسان بله برای مدیریت و صدور لایسنس‌های آفلاین (مخصوص سیستم ادمین).

کتابخانه‌های مجاز: فقط stdlib پایتون + httpx + ماژول license.py
امنیت:
- این فایل و پوشه admin_data و کلید خصوصی هرگز نباید برای مشتری ارسال شوند.
- توکن ربات هرگز در لاگ‌ها چاپ نمی‌شود و در حافظه نگهداری می‌شود.
- دستورات فقط از admin_chat_id پذیرفته می‌شوند و پیام‌های سایر افراد بی‌صدا رد می‌شوند.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

# افزودن ریشه پروژه به sys.path جهت بارگذاری license.py
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import license as lic

# پیکربندی لاگ‌ها
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("admin_bot")

ADMIN_DATA_DIR = ROOT / "admin_data"
BOT_CONFIG_PATH = ADMIN_DATA_DIR / "license_bot.json"
CUSTOMER_CONFIG_PATH = ROOT / "data" / "config.json"
LICENSES_DB_PATH = ADMIN_DATA_DIR / "licenses.json"
OFFSET_PATH = ADMIN_DATA_DIR / "offset.txt"
PRIVKEY_PATH = ADMIN_DATA_DIR / "license_privkey.pem"

BALE_API = "https://tapi.bale.ai"
TEHRAN = timezone(timedelta(hours=3, minutes=30))

# جدول نگاشت ارقام و حروف فارسی/عربی
DIGITS_MAP = str.maketrans({
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
    "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
    "ي": "ی", "ك": "ک",
})


def normalize_text(text: str) -> str:
    """استانداردساز متن: تبدیل ارقام فارسی/عربی به انگلیسی و یکنواخت‌سازی حروف."""
    if not text:
        return ""
    return text.translate(DIGITS_MAP).strip()


def mask_token(text: str, token: str) -> str:
    """ماسک کردن توکن در لاگ‌ها و پیام‌های خطا."""
    if token and token in text:
        return text.replace(token, "[REDACTED_BOT_TOKEN]")
    return text


def ensure_bot_credentials() -> dict[str, str]:
    """بارگذاری مشخصات بات از admin_data/license_bot.json یا ایجاد از data/config.json."""
    ADMIN_DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not BOT_CONFIG_PATH.exists():
        bot_tok = ""
        admin_cid = ""
        if CUSTOMER_CONFIG_PATH.exists():
            try:
                cfg = json.loads(CUSTOMER_CONFIG_PATH.read_text(encoding="utf-8"))
                bale_cfg = cfg.get("bale") or {}
                bot_tok = str(bale_cfg.get("bot_token") or "").strip()
                admin_cid = str(bale_cfg.get("chat_id") or "").strip()
            except Exception as e:
                log.warning("خطا در خواندن data/config.json: %s", e)

        data = {
            "bot_token": bot_tok,
            "admin_chat_id": admin_cid,
        }
        BOT_CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("فایل تنظیمات بات با موفقیت ایجاد شد: %s", BOT_CONFIG_PATH)

    try:
        cfg_data = json.loads(BOT_CONFIG_PATH.read_text(encoding="utf-8"))
        token = str(cfg_data.get("bot_token") or "").strip()
        chat_id = str(cfg_data.get("admin_chat_id") or "").strip()
    except Exception as exc:
        raise RuntimeError(f"خطا در بارگذاری {BOT_CONFIG_PATH}: {exc}") from exc

    if not token:
        raise ValueError(f"توکن ربات در {BOT_CONFIG_PATH} وارد نشده است.")
    if not chat_id:
        raise ValueError(f"شناسه چت ادمین (admin_chat_id) در {BOT_CONFIG_PATH} وارد نشده است.")

    return {"bot_token": token, "admin_chat_id": chat_id}


def load_licenses() -> dict[str, dict[str, Any]]:
    """خواندن لیست لایسنس‌ها از admin_data/licenses.json."""
    if not LICENSES_DB_PATH.exists():
        return {}
    try:
        content = LICENSES_DB_PATH.read_text(encoding="utf-8")
        data = json.loads(content)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        log.error("خطا در بارگذاری پایگاه لایسنس‌ها: %s", exc)
    return {}


def save_licenses(licenses: dict[str, dict[str, Any]]) -> None:
    """ذخیره پایگاه داده لایسنس‌ها با روش اتمیک."""
    ADMIN_DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = LICENSES_DB_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(licenses, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(LICENSES_DB_PATH)


def load_offset() -> int | None:
    """خواندن آفست پیام‌های پردازش‌شده بله."""
    if OFFSET_PATH.exists():
        try:
            return int(OFFSET_PATH.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return None
    return None


def save_offset(offset: int) -> None:
    """ذخیره آفست پیام‌های پردازش‌شده."""
    ADMIN_DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = OFFSET_PATH.with_suffix(".tmp")
    tmp_path.write_text(str(offset), encoding="utf-8")
    tmp_path.replace(OFFSET_PATH)


def is_admin_chat(chat_id: Any, admin_chat_id: str) -> bool:
    """بررسی تطابق شناسه چت پیام با admin_chat_id."""
    if chat_id is None:
        return False
    try:
        return int(str(chat_id).strip()) == int(admin_chat_id.strip())
    except (ValueError, TypeError):
        return str(chat_id).strip() == admin_chat_id.strip()


def handle_command(
    text: str,
    licenses: dict[str, dict[str, Any]],
    privkey: Any,
    now: float | None = None,
) -> tuple[str, bool]:
    """مسیریابی و پردازش دستورات بات (خروجی: متن پاسخ، وضعیت تغییر دیتابیس)."""
    normalized = normalize_text(text)
    if not normalized:
        return "", False

    parts = normalized.split()
    # ترتیب آزاد کلمات: دستور + نام کاربر + عدد روز در هر ترتیبی فهمیده می‌شود
    # (مثل «ساخت karamali 120» یا «120 karamali ساخت»). نام عددی (موبایل) با عدد روز قاطی نمی‌شود.
    _CMD_WORDS = {
        "help": ("help", "راهنما", "start"),
        "list": ("list", "فهرست", "لیست"),
        "status": ("status", "وضعیت", "وضیعت", "info", "check"),
        "make": ("make", "ساخت", "ایجاد", "create", "new"),
        "renew": ("renew", "تمدید", "تمديد", "extend"),
    }
    _CANON = {"help": "help", "list": "list", "status": "وضعیت", "make": "ساخت", "renew": "تمدید"}
    _found_cmd = None
    _rest: list[str] = []
    for _p in parts:
        _hit = next((k for k, words in _CMD_WORDS.items() if _p.lower().lstrip("/") in words), None)
        if _hit and not _found_cmd:
            _found_cmd = _hit
        else:
            _rest.append(_p)
    _day_cands = [x for x in _rest if x.lstrip("+-").isdigit() and 1 <= int(x) <= 3650]
    _num = _day_cands[-1] if _day_cands else None
    _user = next((x for x in _rest if x != _num), None)
    parts = ([_CANON[_found_cmd]] if _found_cmd else []) + ([_user] if _user else []) + ([_num] if _num else [])
    cmd = parts[0].lower().lstrip("/") if parts else ""

    current_dt = datetime.fromtimestamp(now if now is not None else time.time(), tz=TEHRAN)
    today = current_dt.date()
    now_str = current_dt.strftime("%Y-%m-%d %H:%M:%S")

    # دستور راهنما / help
    if cmd in ("help", "راهنما", "start"):
        guide = (
            "🤖 *راهنمای ربات صدور و تمدید لایسنس*\n\n"
            "دستورات مجاز (فارسی و انگلیسی):\n\n"
            "➕ *ساخت لایسنس جدید:*\n"
            "  `ساخت <نام_کاربر> <تعداد_روز>`\n"
            "  `make <user> <days>`\n"
            "  _مثال:_ `ساخت ali 30`\n\n"
            "🔄 *تمدید لایسنس:*\n"
            "  `تمدید <نام_کاربر> <تعداد_روز>`\n"
            "  `renew <user> <days>`\n"
            "  _مثال:_ `تمدید ali 120`\n\n"
            "🔍 *استعلام وضعیت لایسنس:*\n"
            "  `وضعیت <نام_کاربر>`\n"
            "  `status <user>`\n"
            "  _مثال:_ `وضعیت ali`\n\n"
            "📋 *فهرست لایسنس‌ها:*\n"
            "  `فهرست` یا `list`\n\n"
            "❓ *راهنما:*\n"
            "  `راهنما` یا `help`\n\n"
            "💡 *نکته:* ارقام را می‌توانید هم به صورت فارسی (۱۲۰) و هم انگلیسی (120) ارسال نمایید."
        )
        return guide, False

    # دستور فهرست / list
    if cmd in ("list", "فهرست", "لیست"):
        if not licenses:
            return "📂 هیچ لایسنسی در سامانه ثبت نشده است.", False

        lines = [f"📋 *فهرست لایسنس‌ها ({len(licenses)} کاربر):*\n"]
        sorted_items = sorted(licenses.items(), key=lambda item: str(item[1].get("exp") or ""))
        for idx, (uname, rec) in enumerate(sorted_items, 1):
            exp_str = str(rec.get("exp") or "نامشخص")
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                left = (exp_date - today).days
                if left > 0:
                    badge = f"{left} روز باقی‌مانده 🟢"
                elif left == 0:
                    badge = "امروز منقضی می‌شود 🟡"
                else:
                    badge = f"منقضی شده ({abs(left)} روز گذشته) 🔴"
            except Exception:
                badge = "نامعتبر ⚪"
            lines.append(f"{idx}. `{uname}` | 📅 `{exp_str}` | {badge}")

        return "\n".join(lines), False

    # دستور وضعیت / status
    if cmd in ("status", "وضعیت", "وضیعت", "info", "check"):
        if len(parts) < 2:
            return "❌ لطفاً نام کاربری را وارد کنید.\n_مثال:_ `وضعیت ali`", False
        username = parts[1].strip()
        rec = licenses.get(username)
        if not rec:
            return f"❌ کاربر «`{username}`» در سامانه ثبت نشده است.", False

        exp_str = str(rec.get("exp") or "")
        token_prefix = str(rec.get("last_token_prefix") or "---")
        updated_str = str(rec.get("updated") or "---")

        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            left = (exp_date - today).days
            if left > 0:
                st_desc = f"فعال ({left} روز باقی‌مانده)"
                st_icon = "🟢"
            elif left == 0:
                st_desc = "امروز منقضی می‌شود"
                st_icon = "🟡"
            else:
                st_desc = f"منقضی شده ({abs(left)} روز گذشته)"
                st_icon = "🔴"
        except Exception:
            left = -1
            st_desc = "نامعتبر"
            st_icon = "⚪"

        reply = (
            f"📊 *مشخصات لایسنس کاربر:*\n\n"
            f"👤 کاربر: `{username}`\n"
            f"{st_icon} وضعیت: {st_desc}\n"
            f"📅 تاریخ انقضا: `{exp_str}`\n"
            f"⏳ روزهای باقی‌مانده: {left} روز\n"
            f"🔑 پیش‌وند آخرین توکن: `{token_prefix}`\n"
            f"🕒 آخرین به‌روزرسانی: `{updated_str}`"
        )
        return reply, False

    # دستور ساخت / make
    if cmd in ("make", "ساخت", "ایجاد", "create", "new"):
        if len(parts) < 3:
            return "❌ قالب دستور: `ساخت <کاربر> <تعداد_روز>`\n_مثال:_ `ساخت ali 30`", False
        username = parts[1].strip()
        try:
            days = int(parts[2])
        except ValueError:
            return "❌ تعداد روز باید یک عدد معتبر باشد.\n_مثال:_ `ساخت ali 30`", False

        if days < 1 or days > 3650:
            return "❌ تعداد روز باید عددی بین ۱ تا ۳۶۵۰ باشد.", False

        token, exp = lic.issue_token(username, days, privkey=privkey, now=now)
        days_left = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days

        licenses[username] = {
            "exp": exp,
            "last_token_prefix": token[:24] + "...",
            "updated": now_str,
        }

        reply = (
            f"✅ *لایسنس جدید با موفقیت صادر شد.*\n\n"
            f"👤 کاربر: `{username}`\n"
            f"📅 تاریخ انقضا: `{exp}`\n"
            f"⏳ اعتبار: {days_left} روز\n\n"
            f"کد لایسنس (برای کپی لمس کنید):\n"
            f"```{token}```"
        )
        return reply, True

    # دستور تمدید / renew
    if cmd in ("renew", "تمدید", "تمديد", "extend"):
        if len(parts) < 3:
            return "❌ قالب دستور: `تمدید <کاربر> <تعداد_روز>`\n_مثال:_ `تمدید ali 120`", False
        username = parts[1].strip()
        try:
            days = int(parts[2])
        except ValueError:
            return "❌ تعداد روز باید یک عدد معتبر باشد.\n_مثال:_ `تمدید ali 120`", False

        if days < 1 or days > 3650:
            return "❌ تعداد روز باید عددی بین ۱ تا ۳۶۵۰ باشد.", False

        rec = licenses.get(username)
        prev_note = ""
        if rec and rec.get("exp"):
            try:
                curr_exp = datetime.strptime(str(rec["exp"]), "%Y-%m-%d").date()
                if curr_exp >= today:
                    new_exp_date = curr_exp + timedelta(days=days)
                    days_from_today = (new_exp_date - today).days
                    prev_note = f" (تمدید از انقضای قبلی {rec['exp']})"
                else:
                    new_exp_date = today + timedelta(days=days)
                    days_from_today = days
                    prev_note = " (لایسنس قبلی منقضی شده بود؛ تمدید از امروز)"
            except Exception:
                new_exp_date = today + timedelta(days=days)
                days_from_today = days
        else:
            new_exp_date = today + timedelta(days=days)
            days_from_today = days
            prev_note = " (کاربر جدید)"

        token, exp = lic.issue_token(username, days_from_today, privkey=privkey, now=now)
        days_left = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days

        licenses[username] = {
            "exp": exp,
            "last_token_prefix": token[:24] + "...",
            "updated": now_str,
        }

        reply = (
            f"✅ *لایسنس کاربر «`{username}`» با موفقیت تمدید شد.*\n\n"
            f"👤 کاربر: `{username}`\n"
            f"📅 تاریخ انقضای جدید: `{exp}`{prev_note}\n"
            f"⏳ کل اعتبار باقی‌مانده: {days_left} روز (+{days} روز تمدید)\n\n"
            f"کد لایسنس جدید (برای کپی لمس کنید):\n"
            f"```{token}```"
        )
        return reply, True

    return "دستور ناشناخته است.\nبرای مشاهده لیست دستورات، عبارت «راهنما» یا «help» را ارسال فرمایید.", False


def send_bale_message(client: httpx.Client, bot_token: str, chat_id: str, text: str) -> bool:
    """ارسال پیام پاسخ به پیام‌رسان بله."""
    url = f"{BALE_API}/bot{bot_token}/sendMessage"
    try:
        cid_payload: int | str = int(str(chat_id).strip())
    except ValueError:
        cid_payload = str(chat_id).strip()

    try:
        r = client.post(
            url,
            json={
                "chat_id": cid_payload,
                "text": text,
            },
            timeout=25.0,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("ok") is False:
            log.warning("خطا در sendMessage بله: %s", data.get("description"))
            return False
        return True
    except Exception as exc:
        safe_msg = mask_token(str(exc), bot_token)
        log.error("خطا در ارسال پیام به بله: %s", safe_msg)
        return False


def get_bale_updates(
    client: httpx.Client,
    bot_token: str,
    offset: int | None,
    timeout: int = 20,
) -> list[dict[str, Any]]:
    """دریافت به‌روزرسانی‌ها از بله با استفاده از Long Polling."""
    url = f"{BALE_API}/bot{bot_token}/getUpdates"
    params: dict[str, Any] = {
        "timeout": int(timeout),
        "limit": 100,
    }
    if offset is not None:
        params["offset"] = int(offset)

    try:
        r = client.get(url, params=params, timeout=float(timeout) + 15.0)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            return []
        if data.get("ok") is False:
            log.warning("getUpdates بله ناموفق: %s", data.get("description"))
            return []
        res = data.get("result")
        if isinstance(res, list):
            return res
        return []
    except httpx.TimeoutException:
        # پایان مهلت انتظار بدون دریافت پیام (طبیعی در long-poll)
        return []
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            log.warning("تداخل ۴۰۹ بله: احتمالاً نمونه دیگری از ربات در حال اجراست.")
        else:
            safe_err = mask_token(str(exc), bot_token)
            log.error("خطای HTTP در getUpdates: %s", safe_err)
        time.sleep(4)
        return []
    except Exception as exc:
        safe_err = mask_token(str(exc), bot_token)
        log.error("خطای ارتباط با سرور بله: %s", safe_err)
        time.sleep(3)
        return []


def run_self_test() -> bool:
    """تست خودکار و جامع بدون نیاز به شبکه."""
    print("=== شروع تست خودکار و یکپارچه admin_bot.py ===")

    # ۱. تست نرمال‌سازی اعداد و حروف
    test_str = "تمدید user ۱۲۰"
    norm = normalize_text(test_str)
    assert norm == "تمدید user 120", f"خطا در نرمال‌سازی: {norm}"
    print("[1/5] نرمال‌سازی ارقام فارسی/عربی به انگلیسی: تایید شد.")

    # ۲. تست بارگذاری کلید خصوصی و صدور و بررسی توکن Ed25519
    assert PRIVKEY_PATH.exists(), f"کلید خصوصی در {PRIVKEY_PATH} یافت نشد!"
    priv = lic.load_privkey(str(PRIVKEY_PATH))
    token, exp = lic.issue_token("test_buyer", 30, privkey=priv)
    assert token.startswith(lic.TOKEN_PREFIX), "پیش‌وند توکن نامعتبر است."
    verified = lic.verify_token(token)
    assert verified["u"] == "test_buyer", "نام کاربر در توکن همخوانی ندارد."
    assert verified["exp"] == exp, "تاریخ انقضا در توکن همخوانی ندارد."
    print(f"[2/5] صدور و اعتبارسنجی توکن Ed25519: تایید شد (کاربر: {verified['u']}, انقضا: {exp}).")

    # ۳. تست روتر دستورات بدون دستکاری دیتابیس واقعی
    mock_db: dict[str, Any] = {}

    # دستور ساخت
    rep_make, mod_make = handle_command("ساخت ali ۳۰", mock_db, priv)
    assert mod_make is True, "دستور ساخت باید فلگ تغییر دیتابیس را True کند."
    assert "ali" in mock_db, "کاربر ali باید به دیتابیس اضافه شده باشد."
    assert "✅" in rep_make and "ali" in rep_make
    assert mock_db["ali"]["exp"] == exp

    # دستور وضعیت
    rep_status, mod_status = handle_command("وضعیت ali", mock_db, priv)
    assert mod_status is False
    assert "فعال" in rep_status
    assert "ali" in rep_status

    # دستور تمدید ۱۲۰ روز
    rep_renew, mod_renew = handle_command("تمدید ali ۱۲۰", mock_db, priv)
    assert mod_renew is True
    assert "تمدید" in rep_renew
    new_exp = mock_db["ali"]["exp"]
    assert new_exp > exp, "تاریخ انقضای جدید باید پس از تمدید ۱۲۰ روز جلوتر برود."

    # دستور فهرست
    rep_list, mod_list = handle_command("فهرست", mock_db, priv)
    assert mod_list is False
    assert "ali" in rep_list

    # دستور راهنما
    rep_help, mod_help = handle_command("help", mock_db, priv)
    assert mod_help is False
    assert "راهنمای" in rep_help

    # دستور انگلیسی make و renew
    rep_en_make, mod_en_make = handle_command("make user_en 15", mock_db, priv)
    assert mod_en_make is True
    assert "user_en" in mock_db
    rep_en_ren, mod_en_ren = handle_command("renew user_en 30", mock_db, priv)
    assert mod_en_ren is True

    # دستور ناشناخته و اعتبارسنجی ورودی غلط
    rep_bad, _ = handle_command("make invalid_days abc", mock_db, priv)
    assert "عدد" in rep_bad
    rep_zero, _ = handle_command("make zero_days 0", mock_db, priv)
    assert "بین ۱ تا ۳۶۵۰" in rep_zero
    rep_unk, _ = handle_command("invalid_command", mock_db, priv)
    assert "ناشناخته" in rep_unk

    print("[3/5] روتر تمامی دستورات فارسی/انگلیسی و ورودی‌های نامعتبر: تایید شد.")

    # ۴. تست فیلتر امنیت ادمین
    assert is_admin_chat(520397126, "520397126") is True
    assert is_admin_chat("520397126", "520397126") is True
    assert is_admin_chat(999999999, "520397126") is False
    assert is_admin_chat(None, "520397126") is False
    print("[4/5] فیلتر امنیتی فقط ادمین (رد بی‌صدای افراد غیرمجاز): تایید شد.")

    # ۵. تست ساختار ذخیره و بازخوانی دیتابیس {user: {exp, last_token_prefix, updated}}
    ali_entry = mock_db["ali"]
    assert "exp" in ali_entry
    assert "last_token_prefix" in ali_entry
    assert "updated" in ali_entry
    print(f"[5/5] ساختار رکورد دیتابیس تایید شد: {ali_entry}")

    print("=== تمامی مراحل تست خودکار با موفقیت ۱۰۰٪ سپری شدند ===")
    return True


def run_bot() -> None:
    """حلقه اصلی اجرای ربات."""
    creds = ensure_bot_credentials()
    bot_token = creds["bot_token"]
    admin_chat_id = creds["admin_chat_id"]

    if not PRIVKEY_PATH.exists():
        log.error("کلید خصوصی در مسیر %s پیدا نشد.", PRIVKEY_PATH)
        log.error("ابتدا اسکریپت admin_keygen.py را روی این سیستم اجرا کنید.")
        sys.exit(1)

    try:
        privkey = lic.load_privkey(str(PRIVKEY_PATH))
    except Exception as exc:
        log.error("خطا در بارگذاری کلید خصوصی: %s", exc)
        sys.exit(1)

    licenses = load_licenses()
    offset = load_offset()

    log.info("ربات صدور لایسنس شروع به کار کرد.")
    log.info("شناسه چت ادمین: %s", admin_chat_id)
    log.info("تعداد لایسنس‌های موجود: %d", len(licenses))
    if offset is not None:
        log.info("آخرین آفست پیام‌ها: %d", offset)

    with httpx.Client(follow_redirects=True) as client:
        while True:
            try:
                updates = get_bale_updates(client, bot_token, offset, timeout=20)
                if not updates:
                    continue

                for update in updates:
                    up_id = update.get("update_id")
                    if up_id is not None:
                        offset = up_id + 1
                        save_offset(offset)

                    msg = (
                        update.get("message")
                        or update.get("edited_message")
                        or update.get("channel_post")
                    )
                    if not msg or not isinstance(msg, dict):
                        continue

                    chat = msg.get("chat") or {}
                    chat_id = chat.get("id")

                    # اعتبارسنجی امنیتی: فقط پیام‌های ارسالی از ادمین پردازش می‌شوند
                    if not is_admin_chat(chat_id, admin_chat_id):
                        continue

                    raw_text = msg.get("text")
                    if not raw_text:
                        continue

                    log.info("دریافت پیام از ادمین: %s", raw_text[:60])
                    reply_text, modified = handle_command(raw_text, licenses, privkey)

                    if modified:
                        save_licenses(licenses)

                    if reply_text:
                        send_bale_message(client, bot_token, str(chat_id), reply_text)

            except KeyboardInterrupt:
                log.info("توقف ربات توسط کاربر (Ctrl+C).")
                break
            except Exception as exc:
                safe_err = mask_token(str(exc), bot_token)
                log.error("خطای غیرمنتظره در ربات: %s", safe_err)
                time.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="ربات مدیریت و صدور لایسنس دستیار خرید (پیام‌رسان بله)")
    parser.add_argument("--self-test", action="store_true", help="اجرای تست‌های داخلی بدون شبکه")
    args = parser.parse_args()

    if args.self_test:
        success = run_self_test()
        sys.exit(0 if success else 1)

    run_bot()


if __name__ == "__main__":
    main()
