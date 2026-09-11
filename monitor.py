"""Background HTTP checks and Bale messenger notifications (tapi.bale.ai)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from config_store import load_config

log = logging.getLogger("monitor")

# طبق مستند رسمی: https://docs.bale.ai — همان الگوی Bot API
BALE_API = "https://tapi.bale.ai"


def get_bale_credentials(cfg: dict[str, Any]) -> tuple[str, str]:
    """توکن بازو و chat_id؛ در نبود bale از eitaa یا telegram قدیمی."""
    bl = cfg.get("bale") or {}
    tok = (bl.get("bot_token") or "").strip()
    chat = str(bl.get("chat_id") or "").strip()
    if tok and chat:
        return tok, chat
    ea = cfg.get("eitaa") or {}
    t2 = (ea.get("token") or "").strip()
    c2 = str(ea.get("chat_id") or "").strip()
    if t2 and c2:
        return t2, c2
    tg = cfg.get("telegram") or {}
    return (str(tg.get("bot_token") or "").strip(), str(tg.get("chat_id") or "").strip())


def resolve_bale_for_request(bot_token: str | None, chat_id: str | None) -> tuple[str, str]:
    """توکن/چت از بدنهٔ درخواست؛ هر کدام خالی بود از config.json پر می‌شود (نه جایگزینی کامل)."""
    bt = (bot_token or "").strip()
    ch = str(chat_id or "").strip()
    dbt, dch = get_bale_credentials(load_config())
    return (bt or (dbt or "").strip(), ch or str(dch or "").strip())


def _sanitize_bale_markdown_text(text: str) -> str:
    """بله متن را Markdown می‌کند؛ کاراکترهای ویژه در نام کار/URL باعث رد پیام یا خطای پارسر می‌شود."""
    if not text:
        return text
    trans = str.maketrans(
        {
            "*": "＊",
            "_": "＿",
            "`": "＇",
            "[": "［",
            "]": "］",
            "\\": "／",
            "(": "（",
            ")": "）",
        }
    )
    return text.translate(trans)


def _result_suggests_browser_was_closed(r: dict[str, Any]) -> bool:
    """وقتی کاربر پنجره را زود ببندد Playwright معمولاً Target closed برمی‌گرداند."""
    if r.get("playback_window_closed"):
        return True
    err = str(r.get("error") or "").lower()
    return (
        "has been closed" in err
        or "target page, context or browser" in err
        or "browser has been closed" in err
    )


def format_automation_playback_report(
    scenario: dict[str, Any],
    results: list[dict[str, Any]],
    *,
    fallback_title: str = "",
    max_total_length: int = 800,
) -> str:
    """یک پیام خیلی ساده برای کاربر غیرفنی؛ بدون خطای فنی و بدون جزئیات اجرا."""
    meta = scenario.get("meta") if isinstance(scenario.get("meta"), dict) else {}
    name = str((meta or {}).get("name") or "").strip()
    if not name:
        name = str(fallback_title or "").strip()
    if not name:
        su = str(scenario.get("start_url") or "").strip()
        name = (su[:48] + "…") if len(su) > 48 else su if su else "کار ذخیره‌شده"

    n = len(results)
    if n == 0:
        status_line = "کار کامل نشد."
    else:
        ok_count = sum(1 for r in results if bool(r.get("ok")))
        if ok_count == n:
            if n == 1:
                status_line = "کار کامل انجام شد."
            else:
                status_line = f"همهٔ کارها ({n} مورد) کامل انجام شد."
        elif ok_count == 0:
            if n == 1 and _result_suggests_browser_was_closed(results[0]):
                status_line = (
                    "اجرای خودکار به‌خاطر بسته شدن پنجرهٔ مرورگر قطع شد. "
                    "اگر خودت کار را تا آخر جلو بردی همان کافی است؛ وگرنه دوباره اجرا کن."
                )
            elif n == 1:
                status_line = "کار کامل انجام نشد."
            else:
                status_line = "هیچ‌کدام از کارها تا آخر انجام نشد."
        else:
            status_line = f"فقط {ok_count} تا از {n} کار کامل شد؛ بقیه نشد."

    body = (
        "پیام از دستیار جریان کار وب\n\n"
        f"نام کار: {name}\n\n"
        f"وضعیت: {status_line}"
    )

    if len(body) > max_total_length:
        return body[: max_total_length - 20] + "\n…"
    return body


async def bale_send_async(bot_token: str, chat_id: str, text: str) -> None:
    """POST به …/bot{token}/sendMessage همانند Bot API بله."""
    if not bot_token or not chat_id:
        raise ValueError("توکن بازو یا chat_id بله خالی است")

    safe_text = _sanitize_bale_markdown_text(text)
    cid_raw = str(chat_id).strip()
    try:
        chat_payload: int | str = int(cid_raw)
    except ValueError:
        chat_payload = cid_raw

    url = f"{BALE_API}/bot{bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(35.0),
            follow_redirects=True,
            trust_env=True,
        ) as client:
            r = await client.post(
                url,
                json={
                    "chat_id": chat_payload,
                    "text": safe_text,
                    "disable_web_page_preview": False,
                },
            )
            r.raise_for_status()
            try:
                payload = r.json()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"پاسخ نامعتبر از بله: {r.text[:300]}") from exc
            if isinstance(payload, dict):
                ok_val = payload.get("ok")
                if ok_val is False:
                    err = payload.get("description") or payload.get("error_code") or payload
                    raise RuntimeError(f"بله گفت ناموفق: {err}")
                if ok_val is not True and "result" not in payload:
                    log.warning("پاسخ بله بدون ok=true شناخته‌شده: %s", str(payload)[:400])
        log.info(
            "پیام بله sendMessage موفق بود | chat_payload_type=%s",
            type(chat_payload).__name__,
        )
    except httpx.HTTPStatusError as exc:
        body = (exc.response.text or "")[:500]
        log.error("HTTP بله ناموفق | status=%s | body=%s", exc.response.status_code, body)
        raise RuntimeError(f"بله HTTP {exc.response.status_code}: {body}") from exc
    except httpx.RequestError as exc:
        log.error("شبکهٔ بله: %s", exc)
        raise RuntimeError(f"اتصال به سرور بله برقرار نشد: {exc}") from exc


class BaleOtpWaitCancelled(Exception):
    """توقف کاربر هنگام انتظار برای کد پیامک از بله."""


class BaleOtpWaitTimeout(Exception):
    """مهلت انتظار برای کد پیامک از بله تمام شد."""


async def _bale_get_updates_once(
    bot_token: str,
    *,
    offset: int | None = None,
    timeout: int = 0,
    limit: int = 100,
) -> list[dict[str, Any]]:
    url = f"{BALE_API}/bot{bot_token}/getUpdates"
    params: dict[str, Any] = {"timeout": int(timeout), "limit": int(limit)}
    if offset is not None:
        params["offset"] = offset
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(float(timeout) + 15.0),
            follow_redirects=True,
            trust_env=True,
        ) as client:
            r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError as exc:
        body = (exc.response.text or "")[:400]
        raise RuntimeError(f"بله getUpdates HTTP {exc.response.status_code}: {body}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"بله getUpdates شبکه: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("پاسخ getUpdates نامعتبر")
    if data.get("ok") is False:
        raise RuntimeError(f"بله getUpdates ناموفق: {data}")
    res = data.get("result") or []
    return res if isinstance(res, list) else []


def _bale_update_message(update: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(update, dict):
        return None
    return (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
    )


def _bale_chat_id_matches(msg: dict[str, Any], expect_chat_id: str) -> bool:
    ch = msg.get("chat") or {}
    got = ch.get("id")
    if got is None:
        return False
    try:
        return int(str(got)) == int(str(expect_chat_id).strip())
    except ValueError:
        return str(got).strip() == str(expect_chat_id).strip()


def _extract_sms_code_from_text(text: str, lo: int, hi: int) -> str | None:
    if not text:
        return None
    for m in re.finditer(rf"\d{{{lo},{hi}}}", text):
        s = m.group(0)
        if lo <= len(s) <= hi:
            return s
    digits = re.sub(r"\D", "", text)
    if lo <= len(digits) <= hi:
        return digits
    return None


async def bale_drain_pending_updates(bot_token: str) -> int | None:
    """
    صف به‌روزرسانی‌های قدیمی را خالی می‌کند تا فقط پیام بعد از درخواست کد گرفته شود.
    مقدار بازگشتی: offset بعدی برای getUpdates، یا None اگر صف خالی بوده.
    """
    next_off: int | None = None
    while True:
        batch = await _bale_get_updates_once(bot_token, offset=next_off, timeout=0, limit=100)
        if not batch:
            break
        next_off = int(batch[-1].get("update_id") or 0) + 1
    return next_off


async def bale_wait_for_sms_code_reply(
    bot_token: str,
    expect_chat_id: str,
    prompt_text: str,
    *,
    cancel_check: Callable[[], Awaitable[bool]] | None,
    min_digits: int = 4,
    max_digits: int = 8,
    total_timeout_s: float = 600.0,
    long_poll_timeout: int = 25,
) -> str:
    """
    پیام راهنما را می‌فرستد، سپس با getUpdates (مشابه Telegram) منتظر متنی می‌ماند که شامل کد عددی باشد.
    """
    lo = max(4, min(12, int(min_digits)))
    hi = max(lo, min(12, int(max_digits)))

    next_offset: int | None = await bale_drain_pending_updates(bot_token)
    await bale_send_async(bot_token, expect_chat_id, prompt_text)

    deadline = time.monotonic() + max(30.0, float(total_timeout_s))

    while time.monotonic() < deadline:
        if cancel_check and await cancel_check():
            raise BaleOtpWaitCancelled()

        batch = await _bale_get_updates_once(
            bot_token,
            offset=next_offset,
            timeout=long_poll_timeout,
            limit=100,
        )
        for u in batch:
            uid = u.get("update_id")
            if isinstance(uid, int):
                next_offset = uid + 1
            msg = _bale_update_message(u)
            if not isinstance(msg, dict):
                continue
            if not _bale_chat_id_matches(msg, expect_chat_id):
                continue
            body = msg.get("text") or msg.get("caption") or ""
            code = _extract_sms_code_from_text(str(body), lo, hi)
            if code:
                log.info("کد پیامک از بله دریافت شد | len=%s", len(code))
                return code

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

    raise BaleOtpWaitTimeout(
        f"تا {int(total_timeout_s)} ثانیه کدی در چت بله از همین مخاطب دریافت نشد.",
    )


def _effective_url(workflow: dict[str, Any]) -> str | None:
    url = (workflow.get("check_url") or workflow.get("base_url") or "").strip()
    return url or None


async def probe_url(url: str, timeout_seconds: float = 20.0) -> tuple[bool, str]:
    """Return (ok, detail). Uses GET; follows redirects lightly."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False, "آدرس نامعتبر است"
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "SiteWorkflowAssistant/1.0"},
        ) as client:
            r = await client.get(url)
        code = r.status_code
        ok = 200 <= code < 400
        return ok, f"HTTP {code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:400]


async def monitor_loop(stop_event: asyncio.Event) -> None:
    """Poll workflows; notify on transitions to UP (once per workflow)."""
    last_up: dict[str, bool | None] = {}
    poll_default = 60

    while not stop_event.is_set():
        cfg = load_config()
        if not cfg.get("monitoring_enabled"):
            await asyncio.sleep(2)
            continue

        bot_tok, chat = get_bale_credentials(cfg)
        interval = max(15, int(cfg.get("poll_interval_seconds") or poll_default))

        if not bot_tok or not chat:
            log.warning("بله تنظیم نشده؛ مانیتورینگ تا پر کردن توکن/Chat ID صبر می‌کند.")
            await asyncio.sleep(interval)
            continue

        workflows: list[dict[str, Any]] = cfg.get("workflows") or []
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

        for wf in workflows:
            if not wf.get("monitor_this"):
                continue
            wf_id = str(wf.get("id") or "")
            url = _effective_url(wf)
            if not wf_id or not url:
                continue

            ok, detail = await probe_url(url)
            prev = last_up.get(wf_id)

            title = wf.get("name") or wf_id
            if ok and prev is not True:
                msg = (
                    f"✅ سایت برای «{title}» پاسخ داد ({detail}).\n"
                    f"زمان: {now}\n"
                    f"آدرس: {url}\n\n"
                    f"اگر وقت انجام مراحل است، همین الان وارد شوید؛ راهنمای مراحل در برنامه ذخیره شده است."
                )
                try:
                    await bale_send_async(bot_tok, chat, msg)
                    log.info("Notify UP: %s", title)
                except Exception as exc:  # noqa: BLE001
                    log.exception("بله ناموفق برای %s: %s", title, exc)

            elif not ok and prev is True:
                msg_down = (
                    f"⚠️ «{title}» الان در دسترس نیست یا خطا دارد.\n"
                    f"{detail}\nآدرس: {url}"
                )
                try:
                    await bale_send_async(bot_tok, chat, msg_down)
                except Exception as exc:  # noqa: BLE001
                    log.exception("بله (down) خطا: %s", exc)

            last_up[wf_id] = ok

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
