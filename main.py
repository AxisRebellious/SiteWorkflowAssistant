"""Local web UI + API برای تعریف جریان کار سایت‌ها و اعلان بله (tapi.bale.ai)."""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config_store import load_config, save_config
import json
from datetime import datetime, timedelta
import license as lic
from monitor import (
    bale_send_async,
    format_automation_playback_report,
    get_bale_credentials,
    probe_url,
    resolve_bale_for_request,
)

import automation

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

stop_monitor = asyncio.Event()
monitor_task: asyncio.Task[Any] | None = None

playback_cancel_registry: dict[str, asyncio.Event] = {}


async def _bale_try_send(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    attempts: int = 3,
    pause_base_s: float = 2.0,
) -> None:
    """بله را چند بار تلاش می‌کند؛ پخش طولانی گاه بین دو پیام اتصال ناپایدار است."""
    last_exc: BaseException | None = None
    for i in range(max(1, attempts)):
        try:
            await bale_send_async(bot_token, chat_id, text)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning(
                "تلاشٔ ارسال بله %s/%s ناموفق | %s",
                i + 1,
                attempts,
                exc,
            )
            if i < attempts - 1:
                await asyncio.sleep(pause_base_s * (i + 1))
    assert last_exc is not None
    raise last_exc


def _resolve_bale_target(bot_token: str | None, chat_id: str | None) -> tuple[str, str]:
    """ترکیب اعتبارنامهٔ فرم/بدنه با نسخهٔ ذخیره‌شده روی دیسک (فیلدبه‌فیلد)."""
    return resolve_bale_for_request(bot_token, chat_id)


class StepModel(BaseModel):
    title: str = ""
    instruction: str = ""
    selector_hint: str = ""


class SectionModel(BaseModel):
    title: str = ""
    steps: list[StepModel] = Field(default_factory=list)


class WorkflowModel(BaseModel):
    id: str = ""
    name: str = ""
    base_url: str = ""
    check_url: str = ""
    monitor_this: bool = False
    notes: str = ""
    sections: list[SectionModel] = Field(default_factory=list)


class BaleModel(BaseModel):
    bot_token: str = ""
    chat_id: str = ""


class ScenarioQueueItemModel(BaseModel):
    id: str = ""
    script_id: str = ""
    label: str = ""
    start_url: str = ""


class AppConfigBody(BaseModel):
    bale: BaleModel = Field(default_factory=BaleModel)
    poll_interval_seconds: int = 60
    workflows: list[WorkflowModel] = Field(default_factory=list)
    scenario_queue: list[ScenarioQueueItemModel] = Field(default_factory=list)
    monitoring_enabled: bool = False


class ProbeBody(BaseModel):
    url: str


class LicenseActivateBody(BaseModel):
    username: str = ""
    token: str = ""


app = FastAPI(title="SiteWorkflowAssistant", version="1.0.0")


stop_admin_bot = threading.Event()
admin_bot_thread: threading.Thread | None = None


@app.on_event("startup")
async def _startup() -> None:
    global monitor_task, admin_bot_thread

    asyncio.get_event_loop()
    cfg = load_config()
    stop_monitor.clear()
    if cfg.get("monitoring_enabled"):
        from monitor import monitor_loop

        monitor_task = asyncio.create_task(monitor_loop(stop_monitor))

    # در صورتی که کلید ادمین روی این سیستم وجود داشته باشد (سیستم مالک)، ربات بله صدور لایسنس خودکار اجرا می‌شود
    priv_file = ROOT / "admin_data" / "license_privkey.pem"
    bot_file = ROOT / "admin_bot.py"
    if priv_file.is_file() and bot_file.is_file():
        try:
            import admin_bot

            stop_admin_bot.clear()
            admin_bot_thread = admin_bot.start_bot_in_background(stop_admin_bot)
            log.info("ربات پیام‌رسان بله برای مدیریت لایسنس به صورت پس‌زمینه خودکار شروع به کار کرد.")
        except Exception as exc:
            log.warning("عدم امکان راه‌اندازی خودکار ربات بله: %s", exc)


@app.on_event("shutdown")
async def _shutdown() -> None:
    stop_monitor.set()
    stop_admin_bot.set()
    if monitor_task is not None:
        monitor_task.cancel()
    try:
        import automation

        await automation.close_all_sessions()
    except Exception:
        log.exception("automation shutdown")
    finally:
        pass


def _get_license_info() -> dict[str, Any]:
    lic_file = ROOT / "data" / "license.json"
    if not lic_file.is_file():
        return {
            "ok": True,
            "active": False,
            "username": "",
            "expires": "",
            "days_left": 0,
            "message": "فایل لایسنس یافت نشد. لطفاً نرم‌افزار را فعال کنید.",
        }
    try:
        with open(lic_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {
            "ok": True,
            "active": False,
            "username": "",
            "expires": "",
            "days_left": 0,
            "message": "فایل لایسنس معتبر نیست یا آسیب دیده است.",
        }

    token = str(data.get("token") or "").strip()
    username = str(data.get("username") or "").strip()
    if not token:
        return {
            "ok": True,
            "active": False,
            "username": username,
            "expires": "",
            "days_left": 0,
            "message": "کد لایسنس در فایل یافت نشد.",
        }

    try:
        payload = lic.verify_token(token)
    except lic.LicenseError as exc:
        return {
            "ok": True,
            "active": False,
            "username": username,
            "expires": "",
            "days_left": 0,
            "message": f"لایسنس نامعتبر است: {exc}",
        }
    except Exception:
        return {
            "ok": True,
            "active": False,
            "username": username,
            "expires": "",
            "days_left": 0,
            "message": "بررسی امضای لایسنس ناموفق بود.",
        }

    active, msg, left = lic.check_active(payload)
    u = str(payload.get("u") or username)
    exp = str(payload.get("exp") or "")
    return {
        "ok": True,
        "active": active,
        "username": u,
        "expires": exp,
        "days_left": left,
        "message": msg,
    }


def _require_license() -> None:
    info = _get_license_info()
    if not info.get("active"):
        msg = info.get("message") or "لایسنس نرم‌افزار معتبر یا فعال نیست."
        raise HTTPException(status_code=403, detail=msg)


@app.get("/api/license/status")
async def license_status() -> dict[str, Any]:
    return _get_license_info()


@app.post("/api/license/activate")
async def license_activate(body: LicenseActivateBody) -> dict[str, Any]:
    username = (body.username or "").strip()
    token = (body.token or "").strip()

    if not username:
        raise HTTPException(status_code=400, detail="نام کاربری الزامی است.")
    if not token:
        raise HTTPException(status_code=400, detail="کد توکن لایسنس الزامی است.")

    try:
        payload = lic.verify_token(token)
    except lic.LicenseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=400, detail="کد لایسنس معتبر نیست.")

    token_user = str(payload.get("u") or "").strip()
    if token_user.lower() != username.lower():
        raise HTTPException(
            status_code=400,
            detail="نام کاربری واردشده با نام کاربری درون توکن لایسنس مطابقت ندارد.",
        )

    active, msg, left = lic.check_active(payload)
    if not active:
        raise HTTPException(status_code=400, detail=msg or "این توکن لایسنس منقضی شده است.")

    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    lic_file = data_dir / "license.json"
    lic_payload = {
        "username": token_user,
        "token": token,
        "activated_at": datetime.now(lic.TEHRAN).isoformat(),
    }
    try:
        with open(lic_file, "w", encoding="utf-8") as f:
            json.dump(lic_payload, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        log.exception("license_activate write failed")
        raise HTTPException(status_code=500, detail="خطا در ذخیره‌سازی اطلاعات لایسنس.") from exc

    return {
        "ok": True,
        "username": token_user,
        "expires": str(payload.get("exp") or ""),
        "days_left": left,
        "message": msg,
    }


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return load_config()


@app.post("/api/config")
async def post_config(body: AppConfigBody) -> dict[str, Any]:
    out: dict[str, Any] = {
        "bale": body.bale.model_dump(),
        "poll_interval_seconds": max(15, int(body.poll_interval_seconds)),
        "monitoring_enabled": bool(body.monitoring_enabled),
        "workflows": [],
    }

    seen: set[str] = set()
    for wf in body.workflows:
        wid = (wf.id or "").strip()
        if not wid:
            wid = str(uuid.uuid4())
        if wid in seen:
            wid = str(uuid.uuid4())
        seen.add(wid)

        secs = []
        for s in wf.sections:
            secs.append(
                {
                    "title": s.title,
                    "steps": [
                        {
                            "title": st.title,
                            "instruction": st.instruction,
                            "selector_hint": st.selector_hint,
                        }
                        for st in s.steps
                    ],
                }
            )

        out["workflows"].append(
            {
                "id": wid,
                "name": wf.name.strip() or wid[:8],
                "base_url": wf.base_url.strip(),
                "check_url": wf.check_url.strip(),
                "monitor_this": wf.monitor_this,
                "notes": wf.notes,
                "sections": secs,
            }
        )

    out["scenario_queue"] = []
    for q in body.scenario_queue:
        qid = (q.id or "").strip() or str(uuid.uuid4())
        out["scenario_queue"].append(
            {
                "id": qid,
                "script_id": (q.script_id or "").strip(),
                "label": (q.label or "").strip(),
                "start_url": (q.start_url or "").strip(),
            }
        )

    save_config(out)
    log.info("Config saved.")

    if out.get("monitoring_enabled"):
        from monitor import monitor_loop

        global monitor_task

        stop_monitor.set()
        if monitor_task is not None:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
        stop_monitor.clear()
        monitor_task = asyncio.create_task(monitor_loop(stop_monitor))
    else:
        stop_monitor.set()
        if monitor_task is not None:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

    return {"ok": True}


@app.post("/api/probe")
async def post_probe(body: ProbeBody) -> dict[str, Any]:
    url = body.url.strip()
    if not url:
        raise HTTPException(400, "url خالی است")
    ok, detail = await probe_url(url)
    return {"ok": ok, "detail": detail}


class BaleNotifyTestBody(BaseModel):
    message: str = "پیام آزمایشی از برنامهٔ راهنمای جریان کار."
    bot_token: str | None = None
    chat_id: str | None = None


@app.post("/api/bale/test")
async def bale_test(body: BaleNotifyTestBody) -> dict[str, Any]:
    bot_tok, chat = _resolve_bale_target(body.bot_token, body.chat_id)
    if not bot_tok or not chat:
        raise HTTPException(400, "توکن بازو یا Chat ID بله تنظیم نشده؛ در فرم پر کن یا بعد از پر کردن «ذخیرهٔ همه چیز روی دیسک» را بزن.")
    try:
        await bale_send_async(bot_tok, chat, body.message.strip() or ".")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/eitaa/test")
async def eitaa_test_legacy(body: BaleNotifyTestBody) -> dict[str, Any]:
    return await bale_test(body)


@app.post("/api/telegram/test")
async def telegram_test_legacy(body: BaleNotifyTestBody) -> dict[str, Any]:
    return await bale_test(body)


# ----- اتوماسیون مرورگر (Playwright) -----


class AutoStartBody(BaseModel):
    url: str = ""


class AutoSaveBody(BaseModel):
    id: str = ""
    name: str = "سناریو"
    scenario: dict[str, Any]


class PlaybackBody(BaseModel):
    scenario: dict[str, Any]
    runs: list[dict[str, Any]] | None = None
    start_url: str = ""
    pause_between_steps_ms: int | None = None
    playback_health_refresh_interval_s: float | None = None
    notify_bale: bool = True
    bale_bot_token: str | None = None
    bale_chat_id: str | None = None
    scenario_display_name: str = ""
    cancellation_id: str = ""
    turbo_mode: bool = False


class PlaybackCancelBody(BaseModel):
    cancellation_id: str = ""


class PlaybackQueueItemBody(BaseModel):
    script_id: str
    label: str = ""
    start_url: str = ""
    runs: list[dict[str, Any]] | None = None


class PlaybackQueueBody(BaseModel):
    items: list[PlaybackQueueItemBody]
    pause_between_steps_ms: int | None = None
    playback_health_refresh_interval_s: float | None = None
    pause_between_scenarios_s: float = 2.5
    notify_bale: bool = True
    bale_bot_token: str | None = None
    bale_chat_id: str | None = None
    cancellation_id: str = ""
    stop_on_first_error: bool = True
    turbo_mode: bool = False


@app.post("/api/automation/start")
async def automation_start(body: AutoStartBody) -> dict[str, Any]:
    _require_license()
    try:
        sess = await automation.start_browser_session(body.url.strip() or "about:blank")
    except Exception as exc:  # noqa: BLE001
        log.exception("automation_start")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "session_id": sess.id}


@app.post("/api/automation/session/{sid}/poll")
async def automation_poll(sid: str) -> dict[str, Any]:
    sess = automation._sessions.get(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="جلسه پیدا نشد")
    batch = await automation.poll_dom_events(sess)
    return {"ok": True, "batch": batch, "recording": sess.recording}


@app.post("/api/automation/session/{sid}/record/start")
async def automation_record_start(sid: str) -> dict[str, Any]:
    _require_license()
    sess = automation._sessions.get(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="جلسه پیدا نشد")
    await automation.start_recording(sess)
    return {"ok": True}


@app.post("/api/automation/session/{sid}/record/stop")
async def automation_record_stop(sid: str) -> dict[str, Any]:
    sess = automation._sessions.get(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="جلسه پیدا نشد")
    await automation.stop_recording(sess)
    return {"ok": True}


@app.post("/api/automation/session/{sid}/scenario")
async def automation_build_scenario(sid: str) -> dict[str, Any]:
    sess = automation._sessions.get(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="جلسه پیدا نشد")
    await automation.poll_dom_events(sess)
    scen = await automation.build_scenario_from_session(sess)
    return {"ok": True, "scenario": scen}


@app.post("/api/automation/session/{sid}/close")
async def automation_close(sid: str) -> dict[str, Any]:
    await automation.close_session(sid)
    return {"ok": True}


@app.get("/api/automation/session/{sid}/events")
async def automation_events_preview(sid: str) -> dict[str, Any]:
    sess = automation._sessions.get(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="جلسه پیدا نشد")
    await automation.poll_dom_events(sess)
    return {"ok": True, "events": list(sess.events)}


@app.get("/api/automation/scripts")
async def automation_scripts_list() -> dict[str, Any]:
    return {"ok": True, "items": automation.list_saved_scripts()}


@app.get("/api/automation/scripts/{fid}")
async def automation_scripts_get(fid: str) -> dict[str, Any]:
    data = automation.load_script(fid)
    if not data:
        raise HTTPException(status_code=404, detail="فایل نیست")
    return {"ok": True, "script": data}


@app.post("/api/automation/scripts/save")
async def automation_scripts_save(body: AutoSaveBody) -> dict[str, Any]:
    fid = body.id.strip() or str(uuid.uuid4())[:14]
    name = body.name.strip() or fid
    automation.save_script_meta(fid, name, body.scenario)
    return {"ok": True, "id": fid, "name": name}


@app.delete("/api/automation/scripts/{fid}")
async def automation_scripts_delete(fid: str) -> dict[str, Any]:
    automation.delete_script(fid)
    return {"ok": True}


def _playback_title_for_notify(body: PlaybackBody) -> str:
    """عنوان کوتاه برای پیامٔ بله (نام سناریو / متا / آدرس)."""
    t = body.scenario_display_name.strip()
    if t:
        return t
    meta = body.scenario.get("meta") if isinstance(body.scenario.get("meta"), dict) else {}
    t2 = str((meta or {}).get("name") or "").strip()
    if t2:
        return t2
    su = str(body.scenario.get("start_url") or "").strip()
    if su:
        return (su[:48] + "…") if len(su) > 48 else su
    return "سناریو"


@app.post("/api/automation/playback/cancel")
async def automation_playback_cancel(body: PlaybackCancelBody) -> dict[str, Any]:
    cid = body.cancellation_id.strip()
    evt = playback_cancel_registry.get(cid)
    if evt:
        evt.set()
    return {"ok": True}


@app.post("/api/automation/playback")
async def automation_playback(body: PlaybackBody) -> dict[str, Any]:
    _require_license()
    extras: dict[str, Any] = {}
    playback_title = _playback_title_for_notify(body)

    cancel_id = body.cancellation_id.strip() or str(uuid.uuid4())
    cancel_evt = asyncio.Event()
    playback_cancel_registry[cancel_id] = cancel_evt
    extras["cancellation_id"] = cancel_id

    async def playback_cancel_requested() -> bool:
        return cancel_evt.is_set()

    bot_ok, chat_ok = _resolve_bale_target(body.bale_bot_token, body.bale_chat_id)
    bale_otp_creds = (bot_ok, chat_ok) if bot_ok and chat_ok else None

    if body.notify_bale:
        if bot_ok and chat_ok:
            try:
                asyncio.create_task(
                    bale_send_async(
                        bot_ok,
                        chat_ok,
                        (
                            "▶️ شروع اجرای خودکار از دستیار\n"
                            f"کار: {playback_title}\n"
                            "مرورگر پخش در حال باز شدن و انجام سناریو است."
                        ),
                    )
                )
                extras["playback_bale_connect_sent"] = True
            except Exception as exc:  # noqa: BLE001
                log.warning("ارسال پیام آغاز بله برای پخش ناموفق بود: %s", exc)
                extras["playback_bale_start_error"] = str(exc)[:500]
        else:
            extras["playback_bale_report_skipped"] = "توکن بازو یا Chat ID بله خالی بود"

    try:
        result = await automation.run_playback(
            body.scenario,
            body.runs,
            playback_start_url=body.start_url.strip() or None,
            pause_between_steps_ms=body.pause_between_steps_ms,
            playback_health_refresh_interval_s=body.playback_health_refresh_interval_s,
            bale_send=None,
            cancel_check=playback_cancel_requested,
            on_playback_registry_teardown=lambda: playback_cancel_registry.pop(cancel_id, None),
            bale_otp_creds=bale_otp_creds,
            turbo_mode=body.turbo_mode,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("playback")
        automation.get_playback_file_logger().exception(
            "خطای غیرمنتظره در endpoint پخش (HTTP 500 به کلاینت): %s",
            exc,
        )
        playback_cancel_registry.pop(cancel_id, None)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if body.notify_bale:
        res_list = result.get("results") or []
        cancelled = bool(result.get("playback_cancelled"))
        completed_ok = bool(result.get("playback_completed_ok"))
        rows_all_ok = bool(res_list) and all(bool(r.get("ok")) for r in res_list)
        logical_success = completed_ok or (rows_all_ok and not cancelled)
        bot_tail, chat_tail = _resolve_bale_target(body.bale_bot_token, body.bale_chat_id)
        if not bot_tail or not chat_tail:
            extras["playback_bale_end_skipped"] = "پایان: توکن/چت بله بعد از اجرا خالی بود؛ پیام پایان ارسال نشد."
        else:
            try:
                if cancelled:
                    await _bale_try_send(
                        bot_tail,
                        chat_tail,
                        "توقف اجرای سناریو.\nکار: "
                        + playback_title
                        + "\nاجرای خودکار با دستور توقف قطع شد.",
                    )
                    extras["playback_bale_cancel_sent"] = True
                elif logical_success:
                    await _bale_try_send(
                        bot_tail,
                        chat_tail,
                        "پایان کار موفق.\nکار: "
                        + playback_title
                        + "\nسناریو از طرف برنامه بدون خطای گزارش‌شده تمام شد.",
                    )
                    extras["playback_bale_finished_sent"] = True
                else:
                    report = format_automation_playback_report(
                        body.scenario,
                        res_list,
                        fallback_title=playback_title,
                    )
                    await _bale_try_send(bot_tail, chat_tail, report)
                    extras["playback_bale_report_sent"] = True
            except Exception as exc:  # noqa: BLE001
                log.warning("ارسال پیام پایان/خلاصهٔ بله برای پخش ناموفق بود: %s", exc)
                extras["playback_bale_report_error"] = str(exc)[:500]

    return {"ok": True, **result, **extras}


@app.post("/api/automation/playback/queue")
async def automation_playback_queue(body: PlaybackQueueBody) -> dict[str, Any]:
    _require_license()
    if not body.items:
        raise HTTPException(400, "صف سناریو خالی است")

    cancel_id = body.cancellation_id.strip() or str(uuid.uuid4())
    cancel_evt = asyncio.Event()
    playback_cancel_registry[cancel_id] = cancel_evt

    async def playback_cancel_requested() -> bool:
        return cancel_evt.is_set()

    bot_ok, chat_ok = _resolve_bale_target(body.bale_bot_token, body.bale_chat_id)
    bale_otp_creds = (bot_ok, chat_ok) if bot_ok and chat_ok else None

    bale_send_cb = None
    if body.notify_bale and bot_ok and chat_ok:

        async def bale_send_cb(text: str) -> None:
            await bale_send_async(bot_ok, chat_ok, text)

    queue_items = [
        {
            "script_id": it.script_id.strip(),
            "label": it.label.strip(),
            "start_url": it.start_url.strip(),
            "runs": it.runs,
        }
        for it in body.items
        if it.script_id.strip()
    ]
    if not queue_items:
        playback_cancel_registry.pop(cancel_id, None)
        raise HTTPException(400, "هیچ سناریوی معتبر در صف نیست")

    try:
        result = await automation.run_playback_queue(
            queue_items,
            pause_between_steps_ms=body.pause_between_steps_ms,
            playback_health_refresh_interval_s=body.playback_health_refresh_interval_s,
            pause_between_scenarios_s=max(0.0, float(body.pause_between_scenarios_s)),
            bale_send=bale_send_cb,
            cancel_check=playback_cancel_requested,
            on_playback_registry_teardown=lambda: playback_cancel_registry.pop(cancel_id, None),
            bale_otp_creds=bale_otp_creds,
            stop_on_first_error=bool(body.stop_on_first_error),
            turbo_mode=body.turbo_mode,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("playback_queue")
        playback_cancel_registry.pop(cancel_id, None)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    extras: dict[str, Any] = {"cancellation_id": cancel_id}
    if body.notify_bale and (not bot_ok or not chat_ok):
        extras["playback_bale_report_skipped"] = "توکن بازو یا Chat ID بله خالی بود"

    return {"ok": True, **result, **extras}


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# ----- ماژول اختصاصی ایزی‌تریدر (مفید) -----


class EasyTraderBuyBody(BaseModel):
    username: str
    password: str
    stock: str
    qty: str | int = "1"
    dry_run: bool = False
    turbo_mode: bool = True
    cancellation_id: str = ""
    stop_at_search: bool = False
    submit_max_attempts: int = 900
    submit_wait_s: float = 45
    price_mode: str = "max"
    price_value: str = ""
    target_time: str = ""
    target_epoch: float | None = None
    base_url: str | None = None


def parse_target_epoch(target_time: str = "", target_epoch: float | None = None) -> float | None:
    """تبدیل زمان هدف (ISO یا فرمت ساعت HH:MM:SS / HH:MM) یا عدد epoch به تایم‌استمپ ثانیه‌ای."""
    if target_epoch is not None:
        try:
            val = float(target_epoch)
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass
    if not target_time or not str(target_time).strip():
        return None
    s = str(target_time).strip()
    try:
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except Exception:
        pass
    parts = s.split(":")
    if len(parts) in (2, 3):
        try:
            h = int(parts[0])
            m = int(parts[1])
            sec = int(parts[2]) if len(parts) == 3 else 0
            tz = getattr(lic, "TEHRAN", None)
            now = datetime.now(tz) if tz else datetime.now()
            target_dt = now.replace(hour=h, minute=m, second=sec, microsecond=0)
            if target_dt.timestamp() < (now.timestamp() - 7200):
                target_dt += timedelta(days=1)
            return target_dt.timestamp()
        except Exception:
            pass
    return None


def compose_easytrader_scenario(
    username: str,
    password: str,
    stock: str,
    qty: str | int,
    dry_run: bool = False,
    turbo_mode: bool = True,
    stop_at_search: bool = False,
    submit_max_attempts: int = 900,
    submit_wait_s: float = 45,
    price_mode: str = "max",
    price_value: str = "",
    target_epoch: float | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """ساخت خودکار سناریوی خرید در ایزی‌تریدر (مفید) همراه با فال‌بک سلکتورها و مدیریت پاپ‌آپ و خرید آزمایشی."""
    qty_str = str(qty).strip()
    stock_str = stock.strip()

    is_custom = bool(base_url and "m.easytrader.ir" not in base_url)
    clean_base = (base_url or "").rstrip("/")
    entry_url = f"{clean_base}#login" if is_custom else "https://m.easytrader.ir/"
    entry_skip = clean_base.split("/")[-1].split("?")[0].split("#")[0] if is_custom else "m.easytrader.ir"
    login_sub = "login" if is_custom else "login.emofid.com"
    verify_sub = clean_base.split("/")[-1].split("?")[0].split("#")[0] if is_custom else "m.easytrader.ir"
    search_url = f"{clean_base}#/search" if is_custom else "https://m.easytrader.ir/search"

    steps: list[dict[str, Any]] = [
        # ۱. ورود به نشانی PWA ایزی‌تریدر
        {
            "id": "goto_easytrader",
            "type": "goto",
            "url": entry_url,
            "entry_url_base": entry_url,
            "skip_if_url_contains": entry_skip,
            "delay_ms": 0,
            "timeout_ms": 30000,
        },
        # ۲. نام کاربری
        {
            "id": "fill_username",
            "type": "fill",
            "only_if_url_contains": login_sub,
            "selector": "#user-name",
            "selectors": [
                "#user-name",
                "input[name='Username']",
                "#primary_form input[name='Username']",
                "input[name='Username' i]",
                "input[autocomplete='username']",
                "input[type='text']",
            ],
            "value": username,
            "delay_ms": 0,
            "timeout_ms": 45000,
        },
        # ۳. رمز عبور
        {
            "id": "fill_password",
            "type": "fill",
            "only_if_url_contains": login_sub,
            "selector": "#password",
            "selectors": [
                "#password",
                "input[name='Password']",
                "#primary_form input[name='Password']",
                "input[type='password']",
                "input[name='Password' i]",
                "input[autocomplete='current-password']",
            ],
            "value": password,
            "delay_ms": 0,
            "timeout_ms": 45000,
        },
        # ۴. تایید فرم ورود
        {
            "id": "clk_login_submit",
            "type": "click",
            "only_if_url_contains": login_sub,
            "selector": "#primary_form button[type='submit']",
            "selectors": [
                "#primary_form button[type='submit']",
                "button[type='submit']",
                "button:has-text('ورود')",
                "#primary_form button:has-text('ورود')",
                "form#primary_form button",
            ],
            "delay_ms": 0,
            "timeout_ms": 30000,
        },
        # ۵. تأیید ورود: منتظر عبور از صفحهٔ لاگین و auth-callback و ورود به پنل
        {
            "id": "verify_login",
            "type": "expect_url",
            "only_if_url_contains": login_sub,
            "contains": verify_sub,
            "not_contains": "login.emofid.com/Login,auth-callback",
            "label": "تأیید ورود به پنل",
            "timeout_ms": 40000,
        },
        # ۶. بستن پاپ‌آپ اطلاعیه در صورت نمایش در پنل (اختیاری - سریع بدون مکث)
        {
            "id": "clk_close_popup",
            "type": "click",
            "selector": "[data-cy=cancel-action-confirm-btn]",
            "selectors": [
                "[data-cy=cancel-action-confirm-btn]",
                "#cancel-action-confirm-btn",
                "text=بستن",
                "button:has-text('بستن')",
                "button:has-text('انصراف')",
                "[data-cy=modal-close-btn]",
            ],
            "optional": True,
            "timeout_ms": 1500,
            "delay_ms": 0,
        },
        # قرارداد/قوانین (اختیاری): گاهی مودال «متن قرارداد» با دکمه پذیرش می‌آید و
        # روی همه‌چیز می‌نشیند؛ اگر بود پذیرفته می‌شود وگرنه بی‌درنگ رد می‌شود.
        {
            "id": "dismiss_contract_1",
            "type": "click",
            "selector": "button:has-text('موافقم')",
            "selectors": [
                "[role=dialog] button:has-text('موافقم')",
                ".modal button:has-text('موافقم')",
                "button:has-text('موافقم')",
                "[role=dialog] button:has-text('می‌پذیرم')",
                "button:has-text('می‌پذیرم')",
                "[role=dialog] button:has-text('قبول')",
                "button:has-text('قبول')",
            ],
            "optional": True,
            "timeout_ms": 2000,
            "delay_ms": 0,
        },
        # ۷. کلیک روی آیکون جستجو در نوبار
        {
            "id": "clk_search_nav",
            "type": "click",
            "selector": "a[data-cy=main-navbar-search]",
            "selectors": [
                "a[data-cy=main-navbar-search]",
                "a[href='/search']",
                "a[href*='search']",
                "button:has-text('جستجو')",
                "[aria-label*='جستجو']",
            ],
            "skip_if_url_contains": "/search",
            "delay_ms": 0,
            "timeout_ms": 15000,
        },
        # ۷-الف. صبر تا ناوبری به صفحه سرچ کامل شود (SPA دیر می‌رسد؛
        # هر کاری قبل از این روی صفحه قبلی می‌افتد).
        {
            "id": "verify_search_page",
            "type": "expect_url",
            "contains": "/search",
            "label": "ورود به صفحه جستجو",
            "timeout_ms": 15000,
        },
        # ۷-ب. بستن شیت «مجوز اطلاع‌رسانی» در صفحه سرچ (اختیاری).
        # تا اورلی bottom-sheet-overlay هست، تایپ نتیجه‌ای نمی‌دهد («نتیجه‌ای یافت نشد»).
        {
            "id": "dismiss_search_sheet",
            "type": "click",
            "selector": "button:has-text('انصراف')",
            "selectors": [
                "[data-cy=bottom-sheet-overlay] button:has-text('انصراف')",
                ".bottom-sheet button:has-text('انصراف')",
                "button:has-text('انصراف')",
                "[data-cy=bottom-sheet-overlay] button",
                ".bottom-sheet button",
            ],
            "optional": True,
            "timeout_ms": 5000,
            "delay_ms": 0,
        },
        # ۷-ب۲. صبر تا اورلی شیت واقعاً محو شود (انیمیشن بسته شدن)؛
        # تایپ زیر اورلی نیمه‌شفاف به باد می‌رود و «نتیجه‌ای یافت نشد» می‌ماند.
        {
            "id": "wait_sheet_gone",
            "type": "wait_for_hidden",
            "selector": "[data-cy=bottom-sheet-overlay]",
            "selectors": [
                "[data-cy=bottom-sheet-overlay]",
                ".bottom-sheet-overlay",
                ".bottom-sheet",
            ],
            "label": "محو شدن شیت اطلاع‌رسانی",
            "optional": True,
            "timeout_ms": 10000,
        },
        # ۷-ب۳. بارگذاری تازه صفحه سرچ پس از بستن شیت.
        # اگر شیت روی خود /search باز شده باشد، کامپوننت سرچ خراب مقداردهی شده
        # و هیچ تایپی (حتی دوباره) فیلتر را تحریک نمی‌کند؛ ریلود تمیز = حالت سالم.
        {
            "id": "reload_search_clean",
            "type": "goto",
            "url": search_url,
            "delay_ms": 0,
            "timeout_ms": 30000,
        },
        {
            "id": "verify_search_page2",
            "type": "expect_url",
            "contains": "/search",
            "label": "ورود دوباره به صفحه جستجو",
            "timeout_ms": 15000,
        },
        # ۷-ب۴. بستن مجدد شیت در صورت ظاهر شدن پس از ریلود (اختیاری).
        {
            "id": "dismiss_search_sheet2",
            "type": "click",
            "selector": "button:has-text('انصراف')",
            "selectors": [
                "[data-cy=bottom-sheet-overlay] button:has-text('انصراف')",
                ".bottom-sheet button:has-text('انصراف')",
                "button:has-text('انصراف')",
            ],
            "optional": True,
            "timeout_ms": 5000,
            "delay_ms": 0,
        },
        # ۷-ج. صبر تا صفحه سرچ آماده شود (لود لیست نمادها؛ نمای «صنایع منتخب»).
        # تایپ وسط لود اولیه = «نتیجه‌ای یافت نشد» دائمی. اختیاری تا در بدترین حالت ادامه دهد.
        {
            "id": "wait_search_ready",
            "type": "wait_for_selector",
            "selector": "text=صنایع منتخب",
            "selectors": [
                "text=صنایع منتخب",
                "[data-cy='search-panel-result-list']",
            ],
            "optional": True,
            "timeout_ms": 12000,
        },
        # ۸. تایپ نماد در کادر جستجو (تایپ کاراکتر به کاراکتر برای فعال‌سازی جستجوی انگولار؛
        # بدون Enter — اینتر غیرضروری و گاهی مضر است)
        {
            "id": "fill_stock_search",
            "type": "fill",
            "selector": "input[data-cy=layout-search-input]",
            "selectors": [
                "input[data-cy=layout-search-input]",
                "input[placeholder='جستجوی نماد']",
                "#searchInputControl",
                "input[placeholder*='جستجو']",
                "input[type='search']",
            ],
            "value": stock_str,
            "type_text": True,
            "char_delay_ms": 20,
            "press_enter": False,
            "delay_ms": 0,
            "timeout_ms": 15000,
        },
        # ۸-ب. تایپ مجدد ایمنی (پاک + تایپ دوباره).
        # گاهی کلیدهای تایپ اول زیر اورلی/رندر به باد می‌رود و فیلتر در «نتیجه‌ای یافت نشد»
        # گیر می‌کند؛ تایپ دوم روی صفحه آماده، فیلتر را قطعاً تحریک می‌کند. هزینه ~۱ ثانیه.
        {
            "id": "fill_stock_search_retry",
            "type": "fill",
            "selector": "input[data-cy=layout-search-input]",
            "selectors": [
                "input[data-cy=layout-search-input]",
                "input[placeholder='جستجوی نماد']",
                "#searchInputControl",
                "input[placeholder*='جستجو']",
                "input[type='search']",
            ],
            "value": stock_str,
            "type_text": True,
            "char_delay_ms": 20,
            "press_enter": False,
            "delay_ms": 0,
            "timeout_ms": 15000,
        },
        # ۸-ج. عکس تشخیصی از وضعیت سرچ (اختیاری؛ علت‌یابی «نتیجه‌ای یافت نشد»).
        {
            "id": "snap_search_state",
            "type": "snapshot",
            "label": "وضعیت سرچ پس از تایپ",
            "optional": True,
            "timeout_ms": 5000,
        },
        # قرارداد وسط صفحه سرچ (اختیاری؛ جلوی کلیک نتیجه را می‌گیرد).
        {
            "id": "dismiss_contract_2",
            "type": "click",
            "selector": "button:has-text('موافقم')",
            "selectors": [
                "[role=dialog] button:has-text('موافقم')",
                ".modal button:has-text('موافقم')",
                "button:has-text('موافقم')",
                "[role=dialog] button:has-text('می‌پذیرم')",
                "button:has-text('می‌پذیرم')",
                "[role=dialog] button:has-text('قبول')",
                "button:has-text('قبول')",
            ],
            "optional": True,
            "timeout_ms": 2000,
            "delay_ms": 0,
        },
        # ۹. کلیک روی نتیجهٔ جستجو — اول ردیف همان نماد (data-cy نمادمحور؛
        # برای نمادهای پایه مثل خگلپا زیر هدر «سایر»، نه «سهام»).
        # هرگز هدر دسته‌بندی یا wrapper بیرونی (بدون href) کلیک نشود.
        {
            "id": "clk_first_search_result",
            "type": "click",
            "selector": f"[data-cy='search-panel-item-{stock_str}'] a[href*='/stock-details/']",
            "selectors": [
                f"[data-cy='search-panel-item-{stock_str}'] a[href*='/stock-details/']",
                f"[data-cy='search-item-name-{stock_str}']",
                f"a[data-cy^='searchPanelItem']:has-text('{stock_str}')",
                f"a[href*='/stock-details/']:has-text('{stock_str}')",
                f"lib-search-flat-result-list a:has-text('{stock_str}')",
                f"[data-cy='search-panel-result-list'] a:has-text('{stock_str}')",
                f"a:has-text('{stock_str}')",
                "lib-search-flat-result-list a[href*='/stock-details/']",
                "lib-search-flat-result-list a",
                "[data-cy='search-panel-result-list'] a",
            ],
            "delay_ms": 0,
            "timeout_ms": 20000,
        },
        # ۹-ب. تأیید رفتن به صفحه نماد (اگر هنوز روی سرچ هستیم، سریع خطا بده).
        {
            "id": "verify_symbol_page",
            "type": "expect_url",
            "not_contains": "/search",
            "label": "ورود به صفحه نماد",
            "timeout_ms": 15000,
        },
    ]

    if not stop_at_search:
        # ۱۰. کلیک روی دکمه خرید نماد
        steps.append(
            {
                "id": "clk_stock_buy",
                "type": "click",
                "selector": "button[data-cy=order-buy-btn]",
                "selectors": [
                    "button[data-cy=order-buy-btn]",
                    "button[aria-label='خرید']",
                    "button:has-text('خرید')",
                    ".order-buy-btn",
                ],
                "delay_ms": 0,
                "timeout_ms": 15000,
            }
        )
        # ۱۱. پر کردن تعداد/حجم در فرم سفارش
        # قرارداد ابتدای فرم سفارش (اختیاری).
        steps.append(
            {
                "id": "dismiss_contract_3",
            "type": "click",
            "selector": "button:has-text('موافقم')",
            "selectors": [
                "[role=dialog] button:has-text('موافقم')",
                ".modal button:has-text('موافقم')",
                "button:has-text('موافقم')",
                "[role=dialog] button:has-text('می‌پذیرم')",
                "button:has-text('می‌پذیرم')",
                "[role=dialog] button:has-text('قبول')",
                "button:has-text('قبول')",
            ],
            "optional": True,
            "timeout_ms": 2000,
            "delay_ms": 0,
            }
        )
        steps.append(
            {
                "id": "fill_order_qty",
                "type": "fill",
                "selector": "input[data-cy=order-form-input-quantity]",
                "selectors": [
                    "input[data-cy=order-form-input-quantity]",
                    "input#quantity",
                    "input[placeholder*='تعداد']",
                    "input[name*='quantity' i]",
                ],
                "value": qty_str,
                "delay_ms": 0,
                "timeout_ms": 15000,
            }
        )
        _pmode = (price_mode or "max").strip().lower()
        if _pmode not in ("max", "custom"):
            _pmode = "max"
        _pval = str(price_value or "").strip()
        if _pmode == "custom" and not (_pval.isdigit() and int(_pval) > 0):
            _pmode = "max"
            _pval = ""
        _price_selectors = [
            "input[data-cy=order-form-input-price]",
            "input#price",
            ".input-wrapper input[inputmode='decimal']#price",
        ]
        _max_selectors = [
            "div[data-cy=order-form-max-price]",
            ".order-form-max-btn[data-cy=order-form-max-price]",
            "[data-cy=order-form-max-price]",
            ".order-form-max-btn",
        ]
        if _pmode == "custom":
            # ۱۲. درج قیمت دلخواه کاربر در فیلد قیمت (readonly برداشته می‌شود).
            steps.append(
                {
                    "id": "fill_custom_price",
                    "type": "fill",
                    "selector": "input[data-cy=order-form-input-price]",
                    "selectors": _price_selectors,
                    "value": _pval,
                    "delay_ms": 0,
                    "timeout_ms": 15000,
                }
            )
        else:
            # ۱۲. کلیک روی فلش سقف قیمت
            steps.append(
                {
                    "id": "clk_max_price_arrow",
                    "type": "click",
                    "selector": "div[data-cy=order-form-max-price]",
                    "selectors": _max_selectors,
                    "delay_ms": 0,
                    "timeout_ms": 15000,
                }
            )

        if not dry_run:
            # ۱۳. ارسال نهایی تا تأیید ثبت (فقط خرید واقعی): کلیک ارسال → خواندن پیام
            # سامانه → اگر «خارج از ساعت معاملات» بود، صبر و تکرار تا ثبت نهایی.
            # کاربر روشن می‌کند و می‌رود؛ توقف با «توقف اجرا». هرگز recovery ندارد.
            steps.append(
                {
                    "id": "submit_until_filled",
                    "type": "submit_until_confirmed",
                    "no_resume": True,
                    "selector": "button[data-cy=oms-order-form-submit-button-buy]",
                    "selectors": [
                        "button[data-cy=oms-order-form-submit-button-buy]",
                        "button:has-text('ارسال خرید')",
                        "button.btn-success:has-text('ارسال خرید')",
                        "button:has-text('ثبت خرید')",
                    ],
                    "qty_selector": "input[data-cy=order-form-input-quantity]",
                    "qty_selectors": [
                        "input[data-cy=order-form-input-quantity]",
                        "input#quantity",
                    ],
                    "qty_value": qty_str,
                    "max_selector": "" if _pmode == "custom" else "div[data-cy=order-form-max-price]",
                    "max_selectors": [] if _pmode == "custom" else _max_selectors,
                    "price_selector": "input[data-cy=order-form-input-price]" if _pmode == "custom" else "",
                    "price_selectors": _price_selectors if _pmode == "custom" else [],
                    "price_value": _pval if _pmode == "custom" else "",
                    "max_attempts": submit_max_attempts,
                    "wait_s": submit_wait_s,
                    "settle_s": 6,
                    "target_epoch": target_epoch,
                    "delay_ms": 0,
                    "timeout_ms": 15000,
                }
            )
            # عکس متنی لحظه پس از ارسال (مدرک «ثبت شد» یا خطای سایت، بدون نیاز به چشم).
            steps.append(
                {
                    "id": "snap_order_result",
                    "type": "snapshot",
                    "label": "نتیجه ثبت سفارش",
                    "js": "(() => { const b = document.body; return JSON.stringify({ url: location.href, body: (b ? (b.innerText || '').slice(0, 800) : 'NO_BODY') }); })()",
                    "optional": True,
                    "timeout_ms": 5000,
                }
            )

    scenario: dict[str, Any] = {
        "version": 1,
        "start_url": entry_url,
        "entry_url_base": entry_url,
        "turbo_mode": turbo_mode,
        "turbo_refresh": False,
        "reuse_browser": True,
        "keep_browser_open": True,
        # ریکاوری هوشمند: پس از خطا رفرش + ادامه از قدم متناظر با آدرس فعلی.
        # دکمه نهایی خرید عمداً مارکر ندارد تا هرگز دوباره ارسال نشود.
        "recovery_max": 2,
        # مکث پس از موفقیت تا درخواست ثبت سفارش بنشیند (فقط خرید واقعی؛ آزمایشی صفر).
        "settle_before_close_s": 0 if dry_run else 6,
        "resume_markers": [
            {"url_contains": "login.emofid.com", "resume_at": "fill_username"},
            {"url_contains": "/market-watch", "resume_at": "clk_search_nav"},
            {"url_contains": "/search", "resume_at": "dismiss_search_sheet"},
            {"url_contains": "/stock-details/", "resume_at": "clk_stock_buy"},
            {"url_contains": "/order-form/", "resume_at": "fill_order_qty"},
        ],
        "keep_open_on_missing": True,
        "pause_between_steps_ms": 0,
        "pause_between_tasks_ms": 0,
        "show_click_marker": not turbo_mode,
        "auto_solve_captcha": False,
        "steps": steps,
        "meta": {
            "id": "easytrader-buy",
            "name": f"خرید ایزی‌تریدر ({stock_str}) - {'حالت آزمایشی (Dry Run)' if dry_run else 'خرید قطعی'}",
        },
    }

    if dry_run:
        scenario["keep_browser_on_failure"] = True

    return scenario


@app.post("/api/easytrader/buy")
async def easytrader_buy(body: EasyTraderBuyBody) -> dict[str, Any]:
    _require_license()
    username = body.username.strip()
    password = body.password.strip()
    stock = body.stock.strip()
    qty_str = str(body.qty).strip()

    if not username or not password:
        raise HTTPException(status_code=400, detail="نام کاربری و رمز عبور الزامی است.")
    if not stock:
        raise HTTPException(status_code=400, detail="نام یا نماد سهام الزامی است.")
    if not qty_str or qty_str == "0":
        raise HTTPException(status_code=400, detail="تعداد/حجم خرید الزامی و باید بیشتر از صفر باشد.")
    price_mode = (body.price_mode or "max").strip().lower()
    if price_mode not in ("max", "custom"):
        price_mode = "max"
    price_value = str(body.price_value or "").strip()
    if price_mode == "custom" and not (price_value.isdigit() and int(price_value) > 0):
        raise HTTPException(status_code=400, detail="قیمت دلخواه معتبر نیست.")

    cancel_id = body.cancellation_id.strip() or str(uuid.uuid4())
    cancel_evt = asyncio.Event()
    playback_cancel_registry[cancel_id] = cancel_evt

    async def cancel_check() -> bool:
        return cancel_evt.is_set()

    effective_target_epoch = parse_target_epoch(body.target_time, body.target_epoch)

    scenario = compose_easytrader_scenario(
        username=username,
        password=password,
        stock=stock,
        qty=qty_str,
        dry_run=body.dry_run,
        turbo_mode=body.turbo_mode,
        stop_at_search=body.stop_at_search,
        submit_max_attempts=max(1, min(int(body.submit_max_attempts or 900), 2000)),
        submit_wait_s=min(max(float(body.submit_wait_s or 45), 5.0), 300.0),
        price_mode=price_mode,
        price_value=price_value,
        target_epoch=effective_target_epoch,
        base_url=body.base_url,
    )

    try:
        result = await automation.run_playback(
            scenario,
            runs=[{"label": f"خرید {stock} ({'آزمایشی' if body.dry_run else 'واقعی'})", "values": {}}],
            cancel_check=cancel_check,
            on_playback_registry_teardown=lambda: playback_cancel_registry.pop(cancel_id, None),
            turbo_mode=body.turbo_mode,
        )
    except Exception as exc:
        log.exception("easytrader_buy")
        playback_cancel_registry.pop(cancel_id, None)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "ok": True,
        "dry_run": body.dry_run,
        "cancellation_id": cancel_id,
        **result,
    }


@app.post("/api/easytrader/reset_session")
async def easytrader_reset_session() -> dict[str, Any]:
    closed = await automation.reset_shared_playback_context()
    return {"ok": True, "closed": closed}


@app.post("/api/easytrader/cancel")
async def easytrader_cancel(body: PlaybackCancelBody) -> dict[str, Any]:
    cid = body.cancellation_id.strip()
    evt = playback_cancel_registry.get(cid)
    if evt:
        evt.set()
    return {"ok": True}


@app.get("/api/easytrader/logs")
async def easytrader_logs(lines: int = 80) -> dict[str, Any]:
    log_path = automation.get_playback_log_file_path()
    if not log_path.is_file():
        return {"ok": True, "lines": []}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            return {"ok": True, "lines": [ln.rstrip() for ln in all_lines[-lines:]]}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "lines": []}


@app.get("/api/update/check")
async def update_check() -> dict[str, Any]:
    import updater

    try:
        info = updater.check_update(ROOT)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"بررسی آپدیت ناموفق بود: {exc}")
    return {"ok": True, **info}


@app.post("/api/update/apply", status_code=202)
async def update_apply() -> dict[str, Any]:
    import os
    import subprocess
    import sys
    import updater

    if len(playback_cancel_registry) > 0:
        raise HTTPException(
            status_code=409,
            detail="عملیاتی در حال اجراست؛ اول «توقف» بزن بعد آپدیت بگیر.",
        )
    try:
        info = updater.check_update(ROOT)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"بررسی آپدیت ناموفق بود: {exc}")
    if not info.get("available"):
        return {"ok": True, "message": "نرم‌افزار به‌روز است."}
    try:
        last_port = (ROOT / "data" / "last_port.txt").read_text(encoding="utf-8").strip()
        port = int(last_port) if last_port.isdigit() else 9876
    except Exception:
        port = 9876
    try:
        if os.name == "nt":
            subprocess.Popen(
                [sys.executable, "updater.py", "--full-update", "--port", str(port)],
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x00000008,
            )
        else:
            subprocess.Popen(
                [sys.executable, "updater.py", "--full-update", "--port", str(port)],
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"شروع آپدیت ناموفق بود: {exc}")

    async def _suicide() -> None:
        await asyncio.sleep(3)
        os._exit(0)

    asyncio.create_task(_suicide())
    return {"ok": True, "message": "آپدیت شروع شد؛ برنامه خودش بسته و با نسخه جدید باز می‌شود."}


@app.get("/easytrader")
async def easytrader_page() -> FileResponse:
    return FileResponse(str(STATIC / "easytrader.html"))


@app.get("/panel")
async def panel_page() -> FileResponse:
    return FileResponse(str(STATIC / "index.html"))


@app.get("/license")
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC / "license.html"))

