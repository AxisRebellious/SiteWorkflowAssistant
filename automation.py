"""ضبط و پخش سادهٔ مرورگر با Playwright (پنجرهٔ واقعی کرومیوم؛ نه iframe)."""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright._impl._errors import is_target_closed_error

from monitor import (
    BaleOtpWaitCancelled,
    BaleOtpWaitTimeout,
    bale_send_async,
    bale_wait_for_sms_code_reply,
)

log = logging.getLogger("automation")

ROOT = Path(__file__).resolve().parent
AUTOMATION_DIR = ROOT / "data" / "automation"
AUTOMATION_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR = ROOT / "data" / "logs"
PLAYBACK_LOG_FILE = LOGS_DIR / "playback.log"


def get_playback_log_file_path() -> Path:
    """مسیر مطلق فایلٔ لاگ اختصاصی پخش (برای دیباگ)."""
    return PLAYBACK_LOG_FILE.resolve()


def get_playback_file_logger() -> logging.Logger:
    """لاگر یک‌طرفه به data/logs/playback.log — تکرار، خطاها و قطعٔ اتصال."""
    lg = logging.getLogger("automation.playback")
    if not lg.handlers:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            str(PLAYBACK_LOG_FILE),
            maxBytes=4 * 1024 * 1024,
            backupCount=8,
            encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        lg.setLevel(logging.DEBUG)
        lg.addHandler(fh)
        lg.debug(
            "راه‌اندازی فایل لاگ پخش | مسیر=%s",
            PLAYBACK_LOG_FILE.resolve(),
        )
    return lg


class PlaybackCancelled(Exception):
    """توقف به‌درخواست کاربر؛ معمولاً وقتی ارتباط HTTP قطع شده است."""


class PlaybackSessionLost(RuntimeError):
    """پنجره/تبٔ پخش بسته شد یا مرورگر از درایور Playwright جدا شد (قبل از اتمام سناریو)."""


DEFAULT_PLAYBACK_ACTION_TIMEOUT_MS = 60_000
PLAYBACK_CLICK_RETRIES = 2
PLAYBACK_RETRY_PAUSE_S = 0.25

# goto: وقتی سایت موقتاً وصل نیست — با فاصلهٔ ثابت دوباره امتحان می‌شود (پیش‌فرض بدون سقف تلاش)
PLAYBACK_NAV_RETRY_PAUSE_S = 10.0
# هر تلاشٔ باز کردن صفحه حداکثر این‌قدر طول می‌کشد؛ در JSON با nav_retry_goto_timeout_ms عوض کن
PLAYBACK_NAV_GOTO_MS_DEFAULT = 25_000
# سقف ایمنی برای جلوگیری از حلقه بی‌پایان روی URLهای مرده در صورت عدم تعیین سقف توسط کاربر
DEFAULT_NAV_RETRY_DEAD_URL_CAP = 10

# مکث اضافهٔ یکنواخت بعد از هر قدم وقتی در JSON نیست (فرم اجرا هم خالی بماند)
DEFAULT_PAUSE_BETWEEN_STEPS_MS = 0.0
# رفرش صفحه برای امتحان اتصال؛ ۰ = خاموش
DEFAULT_PLAYBACK_HEALTH_REFRESH_INTERVAL_S = 30.0

# solver (anti-bot) default
DEFAULT_SOLVER_BASE_URL = "http://localhost:20128/v1"
DEFAULT_SOLVER_MODEL = "ag/gemini-3-flash"
DEFAULT_SOLVER_RETRY = 2
DEFAULT_AUTO_SOLVE_CAPTCHA = False

TURBO: bool = os.environ.get("TURBO", "").strip().lower() in ("1", "true", "yes", "on")

CHROMIUM_TURBO_ARGS: list[str] = [
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=Translate,TranslateUI,OptimizationHints",
    "--disable-translate",
    "--disable-ipc-flooding-protection",
    "--disable-hang-monitor",
    "--disable-client-side-phishing-detection",
    "--disable-component-extensions-with-background-pages",
    "--disable-default-apps",
    "--mute-audio",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-gpu-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-breakpad",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--no-pings",
]

_recorder_browser: Any | None = None
_playback_shared_context: Any | None = None
_playback_shared_profile_dir: Path | None = None


def _is_context_alive(ctx: Any) -> bool:
    try:
        if ctx is None:
            return False
        b = getattr(ctx, "browser", None)
        if b is not None and hasattr(b, "is_connected") and not b.is_connected():
            return False
        pages = getattr(ctx, "pages", [])
        if not pages:
            return False
        return not pages[0].is_closed()
    except Exception:
        return False


def is_turbo_mode(
    scenario: dict[str, Any] | None = None,
    payload_turbo: bool | None = None,
) -> bool:
    if payload_turbo:
        return True
    if scenario and (bool(scenario.get("turbo_mode")) or bool(scenario.get("turbo_refresh")) or bool(scenario.get("turbo"))):
        return True
    if TURBO:
        return True
    return os.environ.get("TURBO", "").strip().lower() in ("1", "true", "yes", "on")

DEFAULT_SOLVER_PAUSE_AFTER_S = 1.5
# تأخیر کوتاه بعد از کلیک تا اسپیت‌ویو (Vue/React) ورودی شماره را بسازد
PLAYBACK_POST_CLICK_SETTLE_S = 0.0
# پس از رسیدن به /auth قبل از fill — زمان ساخت فیلد #mobile در SPA
PLAYBACK_AUTH_FORM_SETTLE_S = 0.0
# اگر کلیک «ورود» روی صفحهٔ auth دیگر نیست ولی فیلد موبایل آماده است، کلیک رد می‌شود
PLAYBACK_SKIP_CLICK_PROBE_MS = 2800
# پس از کلیک «ارسال کد» — زمان رسیدن پیامک و ساخت صفحهٔ OTP
PLAYBACK_POST_SMS_SUBMIT_SETTLE_S = 0.0

SMS_OTP_BALE_DEFAULT_PROMPT = (
    "برای ادامهٔ اجرای خودکار، کد ورود پیامک‌شده را در همین چت برای بازو بفرستید. "
    "فقط عدد کافی است؛ مثال: 12345"
)


def _step_notify_bale_login_success(st: dict[str, Any]) -> bool:
    """بعد از این قدم موفق، اگر قبلاً کد پیامک از بله گرفته شده پیام «ورود موفق» بروید."""
    return bool(st.get("notify_bale_login_success") or st.get("bale_login_success"))

# بعضیٔ سایت‌ها با beforeunload معطل می‌کنند؛ page/context/browser را با سقف زمان می‌بندیم
_PLAYBACK_CLOSE_PAGE_TIMEOUT_S = 15.0
_PLAYBACK_CLOSE_CONTEXT_TIMEOUT_S = 22.0
_PLAYBACK_CLOSE_BROWSER_TIMEOUT_S = 38.0

# حلقهٔ نقطهٔ کلیک روی خود صفحهٔ سایت (پخش)
MARKER_BRIDGE_JS = r"""
(() => {
  if (window.__SITE_AUTOM_MARKER_V1) return;
  window.__SITE_AUTOM_MARKER_V1 = true;
  const css = document.createElement("style");
  css.textContent = `
    #__site_autom_marker_ring {
      position: fixed;
      pointer-events: none !important;
      z-index: 2147483646;
      left: 0;
      top: 0;
      width: 52px;
      height: 52px;
      margin-left: -26px;
      margin-top: -26px;
      box-sizing: border-box;
      border-radius: 50%;
      border: 4px solid rgba(255, 70, 40, 0.95);
      box-shadow:
        0 0 0 3px rgba(255, 230, 100, 0.55),
        0 0 42px 10px rgba(255, 90, 30, 0.42),
        inset 0 0 18px rgba(255, 200, 120, 0.35);
      background: radial-gradient(circle, rgba(255, 220, 100, 0.28) 0%, transparent 72%);
      animation: __sa_marker_pulse 0.95s ease-in-out infinite;
    }
    #__site_autom_marker_ring.__sa_fill {
      border-color: rgba(45, 140, 255, 0.92);
      box-shadow:
        0 0 0 3px rgba(150, 210, 255, 0.5),
        0 0 36px 8px rgba(50, 150, 255, 0.38),
        inset 0 0 16px rgba(180, 220, 255, 0.3);
      background: radial-gradient(circle, rgba(120, 190, 255, 0.22) 0%, transparent 70%);
      animation-duration: 0.75s;
    }
    @keyframes __sa_marker_pulse {
      0%,
      100% {
        transform: scale(0.88);
        opacity: 1;
      }
      50% {
        transform: scale(1.12);
        opacity: 0.9;
      }
    }
  `;
  document.documentElement.appendChild(css);
  function ringEl() {
    let el = document.getElementById("__site_autom_marker_ring");
    if (!el) {
      el = document.createElement("div");
      el.id = "__site_autom_marker_ring";
      document.documentElement.appendChild(el);
    }
    return el;
  }
  window.__SITE_AUTOM_MARKER_SHOW = function (cx, cy, mode) {
    const el = ringEl();
    el.classList.toggle("__sa_fill", mode === "fill");
    el.style.display = "block";
    el.style.left = cx + "px";
    el.style.top = cy + "px";
    el.style.opacity = "1";
  };
  window.__SITE_AUTOM_MARKER_HIDE = function (delayMs) {
    const hide = () => {
      const el = document.getElementById("__site_autom_marker_ring");
      if (el) el.style.display = "none";
    };
    const d = Number(delayMs) || 0;
    if (d > 0) setTimeout(hide, d);
    else hide();
  };
})();
"""


def _scenario_show_visual_marker(scenario: dict[str, Any]) -> bool:
    v = scenario.get("show_click_marker")
    if v is None:
        return True
    return bool(v)


async def _ensure_marker_bridge(page: Any) -> None:
    try:
        await page.evaluate(MARKER_BRIDGE_JS)
    except Exception:
        log.debug("نشانگر بصری نصب نشد", exc_info=True)


async def _show_action_marker(
    page: Any,
    loc: Any,
    *,
    mode: str,
    timeout_ms: int,
) -> None:
    try:
        await _ensure_marker_bridge(page)
        await loc.scroll_into_view_if_needed(timeout=min(12_000, timeout_ms))
        box = await loc.bounding_box()
        if not box:
            return
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        await page.evaluate(
            """([cx, cy, mode]) => {
          if (window.__SITE_AUTOM_MARKER_SHOW) window.__SITE_AUTOM_MARKER_SHOW(cx, cy, mode);
        }""",
            [cx, cy, mode],
        )
    except Exception:
        log.debug("نمایش نشانگر دور المان", exc_info=True)


async def _hide_action_marker(page: Any, delay_ms: int = 500) -> None:
    try:
        await page.evaluate(
            """(d) => {
          if (window.__SITE_AUTOM_MARKER_HIDE) window.__SITE_AUTOM_MARKER_HIDE(d);
        }""",
            int(max(0, delay_ms)),
        )
    except Exception:
        pass


RECORD_PAGE_JS = r"""
(() => {
  try {
    if (document.documentElement.__siteAutomAttached === true) return;
    Object.defineProperty(document.documentElement, "__siteAutomAttached", { value: true, configurable: true });
  } catch (e) {
    if (document.__siteAutomAttached) return;
    document.__siteAutomAttached = true;
  }
  window.__SITE_REC_EVENTS = window.__SITE_REC_EVENTS || [];

  function cssEscape(s) {
    try {
      if (typeof CSS !== "undefined" && CSS.escape) return CSS.escape(s);
    } catch (e) {}
    return String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function escAttr(s) {
    return String(s || "").replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  }

  function cssPath(el) {
    if (!el || el.nodeType !== 1) return "";
    try {
      var root = document.documentElement;
      if (!root || !document.body.contains(el)) return "";
    } catch (e) {}
    try {
      if (el.id) return "#" + cssEscape(el.id);
      var dt = el.getAttribute && el.getAttribute("data-testid");
      if (dt) return "[data-testid=\"" + escAttr(dt) + "\"]";
      var nm = el.getAttribute && el.getAttribute("name");
      if (nm && /^[a-zA-Z0-9_-]+$/.test(nm)) return el.tagName.toLowerCase() + "[name=\"" + escAttr(nm) + "\"]";
    } catch (e) {}
    const parts = [];
    let cur = el;
    for (let d = 0; d < 10 && cur && cur.nodeType === 1; d++) {
      let sel = cur.tagName.toLowerCase();
      if (cur.id) {
        parts.unshift("#" + cssEscape(cur.id));
        break;
      }
      let nth = 1;
      let s = cur;
      while ((s = s.previousElementSibling)) {
        if (s.tagName === cur.tagName) nth++;
      }
      if (nth > 1) sel += ":nth-of-type(" + nth + ")";
      parts.unshift(sel);
      cur = cur.parentElement;
    }
    return parts.join(" > ");
  }

  function push(ev) {
    try {
      ev.t = Date.now();
      window.__SITE_REC_EVENTS.push(ev);
    } catch (e) {}
  }

  document.addEventListener(
    "click",
    function (e) {
      let t = e.target;
      if (!t || t.nodeType !== 1) return;
      if (t.tagName === "INPUT" || t.tagName === "TEXTAREA") return;
      let sel = cssPath(t);
      if (!sel) return;
      push({ type: "click", selector: sel, tag: t.tagName });
    },
    true,
  );

  document.addEventListener(
    "change",
    function (e) {
      let t = e.target;
      if (!t || !("value" in t)) return;
      if (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT") {
        let sel = cssPath(t);
        if (!sel) return;
        push({ type: "fill", selector: sel, value: String(t.value || ""), tag: t.tagName });
      }
    },
    true,
  );

  document.addEventListener(
    "blur",
    function (e) {
      let t = e.target;
      if (!t || (t.tagName !== "INPUT" && t.tagName !== "TEXTAREA")) return;
      let sel = cssPath(t);
      if (!sel) return;
      push({ type: "fill", selector: sel, value: String(t.value || ""), tag: t.tagName });
    },
    true,
  );

  /* هر بار تایپ: خیلی از فرم‌ها تا blur/change نمی‌رسند اگر کاربر مستقیم سراغ دکمهٔ «ارسال کد» برود. */
  document.addEventListener(
    "input",
    function (e) {
      let t = e.target;
      if (!t || !("value" in t)) return;
      if (t.tagName !== "INPUT" && t.tagName !== "TEXTAREA") return;
      let sel = cssPath(t);
      if (!sel) return;
      push({ type: "fill", selector: sel, value: String(t.value || ""), tag: t.tagName });
    },
    true,
  );

  /* آخرین مقدار فیلدِ در فوکوس را قبل از کلیک روی دکمهٔ بعدی بفرستیم (بدون خروج از فیلد). */
  document.addEventListener(
    "pointerdown",
    function () {
      var ae = document.activeElement;
      if (!ae || !("value" in ae)) return;
      if (ae.tagName !== "INPUT" && ae.tagName !== "TEXTAREA") return;
      let sel = cssPath(ae);
      if (!sel) return;
      push({ type: "fill", selector: sel, value: String(ae.value || ""), tag: ae.tagName });
    },
    true,
  );
})();
"""


@dataclass
class RecorderSession:
    id: str
    browser: Any
    context: Any
    page: Any
    events: list[dict[str, Any]] = field(default_factory=list)
    recording: bool = False
    _last_nav: str | None = None


_sessions: dict[str, RecorderSession] = {}
_pw: Any = None


def _sanitize_url(raw: str) -> str:
    u = raw.strip()
    if not u or u.lower() == "about:blank":
        return "about:blank"
    if re.match(r"^(https?|file|data|about|ftp)://?", u, re.I):
        return u
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


def _playback_resolve(text: str, values: dict[str, Any]) -> str:
    if not isinstance(text, str):
        text = str(text)

    def repl(m: re.Match[str]) -> str:
        key = (m.group(1) or "").strip()
        if key in values and values[key] is not None:
            return str(values[key])
        return m.group(0)

    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", repl, text)


async def launch_playwright() -> Any:
    global _pw
    # Ensure PLAYWRIGHT_BROWSERS_PATH is automatically set to portable runtime/browsers if present
    base_dir = Path(__file__).resolve().parent
    local_browsers = base_dir / "runtime" / "browsers"
    if local_browsers.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(local_browsers))

    if _pw is None:
        try:
            from playwright.async_api import async_playwright
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "پیش‌نیاز ناقص است؛ در همین محیط اجرا کن:\n"
                "  pip install playwright\n"
                "  python -m playwright install chromium\n"
                "بعد run.bat یا uvicorn را دوباره اجرا کن."
            ) from exc

        _pw = await async_playwright().start()
    return _pw


async def get_or_launch_recorder_browser() -> Any:
    global _recorder_browser
    pw = await launch_playwright()
    if _recorder_browser is None or not _recorder_browser.is_connected():
        _recorder_browser = await pw.chromium.launch(
            headless=False,
            args=CHROMIUM_TURBO_ARGS,
        )
    return _recorder_browser


async def start_browser_session(start_url: str) -> RecorderSession:
    browser = await get_or_launch_recorder_browser()
    sid = uuid.uuid4().hex[:16]
    url = _sanitize_url(start_url)
    log.info("Launch Chromium session %s", sid)
    context = await browser.new_context(locale="fa-IR", viewport={"width": 1280, "height": 800})
    await context.add_init_script(RECORD_PAGE_JS)
    page = await context.new_page()
    sess = RecorderSession(id=sid, browser=browser, context=context, page=page)

    def on_navigated(frame: Any) -> None:
        try:
            if frame.parent_frame is not None:
                return
        except Exception:
            return
        try:
            u = frame.url or ""
        except Exception:
            return
        if not str(u).startswith(("http:", "https:")):
            return
        if sess._last_nav == u:
            return
        sess._last_nav = u
        sess.events.append({"type": "goto", "url": str(u), "t": time.time() * 1000})

    page.on("framenavigated", on_navigated)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=38000)
    except Exception:
        log.warning("domcontentloaded نشد؛ تلاش دوم با commit")
        try:
            await page.goto(url, wait_until="commit", timeout=22000)
        except Exception as exc:
            log.warning("نشانی باز نشد؛ تبٔ خالی باز می‌شود تا حداقل مرورگر دیده شود: %s", exc)
            await page.goto("about:blank", wait_until="domcontentloaded", timeout=15000)

    sess.events.clear()
    try:
        sess._last_nav = sess.page.url
    except Exception:
        sess._last_nav = url

    _sessions[sid] = sess
    return sess


async def poll_dom_events(sess: RecorderSession) -> list[dict[str, Any]]:
    """رویدادهای هر فریم را می‌خواند (فیلدهای داخل iframe در windowٔ اصلی دیده نمی‌شوند)."""
    combined: list[dict[str, Any]] = []
    try:
        frames = list(sess.page.frames)
    except Exception:
        frames = []
    if not frames:
        try:
            frames = [sess.page.main_frame]
        except Exception:
            frames = []
    for fr in frames:
        try:
            batch = await fr.evaluate(
                """() => {
          if (!window.__SITE_REC_EVENTS) return [];
          return window.__SITE_REC_EVENTS.splice(0);
        }"""
            )
        except Exception as exc:
            log.debug("poll dom frame رد شد: %s", exc)
            continue
        if not batch:
            continue
        for it in batch:
            if isinstance(it, dict):
                combined.append(it)
                sess.events.append(it)
    return combined


async def start_recording(sess: RecorderSession) -> None:
    sess.recording = True
    try:
        frames = list(sess.page.frames)
    except Exception:
        frames = []
    for fr in frames:
        try:
            await fr.evaluate("window.__SITE_REC_EVENTS=[]; undefined;")
        except Exception:
            pass


async def stop_recording(sess: RecorderSession) -> None:
    await poll_dom_events(sess)
    sess.recording = False


def _merge_dom_events_to_steps(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    evs = list(events)
    evs.sort(key=lambda x: float(x.get("t") or 0))

    seq = 0
    prev_goto: str | None = None

    def next_id(prefix: str) -> str:
        nonlocal seq
        seq += 1
        return f"{prefix}_{seq}"

    for ev in evs:
        ty = ev.get("type") or ""

        if ty == "goto":
            u = (ev.get("url") or "").strip()
            if not u.startswith(("http:", "https:")):
                continue
            if prev_goto == u:
                continue
            merged.append({"id": next_id("goto"), "type": "goto", "url": u, "delay_ms": 450})
            prev_goto = u

        elif ty == "click" and ev.get("selector"):
            merged.append({"id": next_id("clk"), "type": "click", "selector": ev["selector"], "delay_ms": 400})

        elif ty == "fill" and ev.get("selector"):
            sel = ev["selector"]
            val = str(ev.get("value") or "")
            last = merged[-1] if merged else None
            if last and (last.get("type") or "").lower() == "fill" and last.get("selector") == sel:
                if last.get("value") == val:
                    continue
                last["value"] = val
                continue
            merged.append(
                {
                    "id": next_id("fill"),
                    "type": "fill",
                    "selector": sel,
                    "value": val,
                    "param": None,
                    "delay_ms": 250,
                }
            )

    return merged


def _urls_equivalent(a: str, b: str) -> bool:
    x = (a or "").strip()
    y = (b or "").strip()
    if x == y:
        return True
    return x.rstrip("/") == y.rstrip("/")


def _resolve_playback_http_url(url_expr: str, defaults: dict[str, Any]) -> str | None:
    """آدرس نهایی http(s) برای پخش؛ None اگر غیرقابل‌استفاده."""
    raw = (url_expr or "").strip()
    if not raw:
        return None
    resolved = _playback_resolve(raw, defaults).strip()
    u = _sanitize_url(resolved)
    lu = u.lower().strip()
    if lu.startswith("about:"):
        return None
    if not (lu.startswith("http://") or lu.startswith("https://")):
        return None
    if "{{" in u and "}}" in u:
        return None
    return u


def _prepend_goto(steps: list[dict[str, Any]], url: str) -> list[dict[str, Any]]:
    """اگر اولین قدم goto به همین url نیست، یک goto به ابتدای سناریو می‌چسباند."""
    u = _sanitize_url(url.strip())
    if not u.startswith(("http:", "https:")):
        return steps
    out = list(steps)
    if (
        out
        and (out[0].get("type") or "").lower() == "goto"
        and _urls_equivalent(str(out[0].get("url") or ""), u)
    ):
        return out
    return [{"id": "goto_enter", "type": "goto", "url": u, "delay_ms": 450}, *out]


async def build_scenario_from_session(sess: RecorderSession) -> dict[str, Any]:
    """
    ناوبری اول پس از بازکردن مرورگر در events پاک شده؛ اگر کاربر دوباره navigate نکند،
    سناریو بدون goto می‌ماند. اینجا با آدرس فعلی تب یک goto در صورت نیاز می‌چسبانیم.
    """
    merged = _merge_dom_events_to_steps(list(sess.events))
    cur = ""
    try:
        cur = (await sess.page.url) or ""
    except Exception:
        pass
    cur = cur.strip()
    start_url = ""
    if cur.startswith(("http:", "https:")):
        start_url = _sanitize_url(cur)
        if not merged or (merged[0].get("type") or "").lower() != "goto":
            merged = _prepend_goto(merged, start_url)

    return {
        "version": 1,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "default_values": {},
        "pause_between_tasks_ms": 1500,
        "pause_between_steps_ms": int(DEFAULT_PAUSE_BETWEEN_STEPS_MS),
        "start_url": start_url or None,
        "show_click_marker": True,
        "steps": merged,
        "runs": [{"label": "بار نخست", "values": {}}],
        "notes": "برای مقداری که هر بار فرق می‌کند پارام ست کن (مثل amount): در هر fill فیلٔ param را پر کن و در runs همان کلید را بگذار. برای کلیک یا پر کردن کند timeout_ms بگذار. مکث یکنواخت بعد از هر قدم: pause_between_steps_ms یا فرم اجرا (اگر در JSON نبود پیش‌فرض سرور ۱۵۰۰ms). playback_health_refresh_interval_s: ثانیه بین رفرش خودکار صفحه برای امتحان اتصال (۰=خاموش؛ پیش‌فرض سرور ۳۰؛ روی نشانی‌های auth/login/otp رفرش کامل خاموش تا SPA ورود قطع نشود). start_url برای ورود. show_click_marker: true نشانگر روی کلیک/پر کردن. باز کردن صفحه و همچنین انتظار برای آماده‌شدن المان در کلیک/پر کردن وقتی سایت کند یا قطع است: nav_retry_pause_seconds (ثانیه بین تلاش‌ها، پیش‌فرض 10)، nav_retry_goto_timeout_ms (میلی‌ثانیهٔ هر تلاش برای goto؛ برای کلیک/fill همان timeout_ms قدم یا همین سقف کمینهٔ داخلی)، برای سقف شمارٔ تلاش nav_retry_max_attempts (≥۲ یا حذف برای نامحدود؛ ۱ نادیده گرفته می‌شود). پیش‌فرض بعد از خطا مرورگر بسته می‌شود؛ با keep_browser_on_failure: true پنجرهٔ خطا باز بماند. مسیرهای nth-of-type شکننده‌اند. پخش با کد پیامک از بله: روی قدمٔ fill که کد یک‌بار مصرف است true بگذار sms_otp یا sms_otp_fill؛ در فرم پخش توکن بازو و chat_id بله را بده (همان چتی که کد را می‌فرستی). sms_otp_wait_seconds در ریشهٔ سناریو — سقف ثانیه انتظار برای کد (۳۰…۷۲۰۰؛ پیش‌فرض ۶۰۰). اختیاری روی همان قدم: sms_otp_prompt، sms_otp_min_digits، sms_otp_max_digits، sms_otp_wait_seconds (اولویت با قدم اگر پر باشد). برای پیام «ورود موفق» به بله بعد از OTP: روی قدمٔ مناسب (مثل کلیک تأیید) notify_bale_login_success یا bale_login_success: true (فقط وقتی قبلاً از بله کد گرفته شده ارسال می‌شود). شمارهٔ موبایل در ضبط با تایپ و پیش از کلیک روی دکمه ثبت می‌شود؛ sms_otp را فقط روی فیلد کد پیامک بگذار نه روی فیلد شماره.",
    }


def playback_steps_with_entry(
    scenario: dict[str, Any],
    *,
    playback_start_url: str | None,
) -> list[dict[str, Any]]:
    """
    همیشه قبل از کلیک‌ها باید یک goto با آدرس http واقعی باشد.
    اولویت آدرس: پارامتر اجرای API، سپس کلید start_url سناریو.
    اگر اولین قدم قبلاً goto باشد ولی با این آدرس‌ها هم‌خوان نباشد یا url نامعتبر باشد،
    همان قدم اول اصلاح می‌شود.
    """
    defaults = dict(scenario.get("default_values") or {})
    steps = list(scenario.get("steps") or [])

    preferred = _resolve_playback_http_url(playback_start_url or "", defaults)
    if preferred is None:
        preferred = _resolve_playback_http_url(str(scenario.get("entry_url_base") or ""), defaults)
    if preferred is None:
        preferred = _resolve_playback_http_url(str(scenario.get("start_url") or ""), defaults)

    if not steps:
        if preferred is not None:
            return _prepend_goto([], preferred)
        return []

    ty0 = (steps[0].get("type") or "").lower()
    if ty0 != "goto":
        if preferred is not None:
            return _prepend_goto(steps, preferred)
        return steps

    current = _resolve_playback_http_url(str(steps[0].get("url") or ""), defaults)

    if preferred is not None:
        if current is None or not _urls_equivalent(current, preferred):
            s0 = dict(steps[0])
            s0["url"] = preferred
            return [s0, *steps[1:]]
        return steps

    return steps


async def close_session(session_id: str) -> None:
    sess = _sessions.pop(session_id, None)
    if not sess:
        return
    try:
        await sess.context.close()
    except Exception:
        pass
    if sess.browser and sess.browser != _recorder_browser:
        try:
            await sess.browser.close()
        except Exception:
            pass


async def reset_shared_playback_context() -> bool:
    """بستن کامل نشست مرورگر اشتراکی و پاکسازی پروفایل موقت آن."""
    global _playback_shared_context, _playback_shared_profile_dir
    closed = False
    if _playback_shared_context is not None:
        try:
            await _playback_shared_context.close()
            closed = True
        except Exception:
            pass
        _playback_shared_context = None
    if _playback_shared_profile_dir is not None:
        try:
            shutil.rmtree(_playback_shared_profile_dir, ignore_errors=True)
        except Exception:
            pass
        _playback_shared_profile_dir = None
    return closed


async def close_all_sessions() -> None:
    global _recorder_browser, _playback_shared_context, _playback_shared_profile_dir
    for sid in list(_sessions.keys()):
        await close_session(sid)
    if _recorder_browser is not None:
        try:
            if _recorder_browser.is_connected():
                await _recorder_browser.close()
        except Exception:
            pass
        _recorder_browser = None
    await reset_shared_playback_context()


def list_saved_scripts() -> list[dict[str, Any]]:
    AUTOMATION_DIR.mkdir(parents=True, exist_ok=True)
    idx = AUTOMATION_DIR / "scripts_index.json"
    items_map: dict[str, str] = {}
    if idx.exists():
        try:
            data = json.loads(idx.read_text(encoding="utf-8"))
            for row in data.get("items") or []:
                if row.get("id"):
                    items_map[str(row["id"])] = str(row.get("name") or row["id"])
        except Exception:
            pass

    # Discover all JSON files directly in AUTOMATION_DIR in case scripts_index is incomplete or missing
    for p in AUTOMATION_DIR.glob("*.json"):
        if p.name == "scripts_index.json":
            continue
        fid = p.stem
        if fid not in items_map:
            try:
                sc = json.loads(p.read_text(encoding="utf-8"))
                m = sc.get("meta") or {}
                name = m.get("name") or fid
            except Exception:
                name = fid
            items_map[fid] = str(name)

    items = [{"id": k, "name": v} for k, v in items_map.items()]
    # Update index if needed
    try:
        idx.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return items


def load_script(fid: str) -> dict[str, Any] | None:
    fp = AUTOMATION_DIR / f"{fid}.json"
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_script_meta(fid: str, name: str, scenario: dict[str, Any]) -> None:
    fp = AUTOMATION_DIR / f"{fid}.json"
    payload = dict(scenario)
    meta = dict(payload.get("meta") or {})
    meta.update({"id": fid, "name": name, "saved": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    payload["meta"] = meta
    fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    items = list_saved_scripts()
    found = False
    for row in items:
        if row.get("id") == fid:
            row["name"] = name
            found = True
            break
    if not found:
        items.append({"id": fid, "name": name})
    (AUTOMATION_DIR / "scripts_index.json").write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def delete_script(fid: str) -> None:
    fp = AUTOMATION_DIR / f"{fid}.json"
    fp.unlink(missing_ok=True)
    items = [r for r in list_saved_scripts() if r.get("id") != fid]
    (AUTOMATION_DIR / "scripts_index.json").write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _step_timeout_ms(st: dict[str, Any]) -> int:
    v = st.get("timeout_ms")
    if v is None or v == "":
        return DEFAULT_PLAYBACK_ACTION_TIMEOUT_MS
    try:
        t = int(float(v))
        return max(1000, min(t, 300_000))
    except Exception:
        return DEFAULT_PLAYBACK_ACTION_TIMEOUT_MS


def _is_playwright_window_closed_error(exc: BaseException) -> bool:
    """صفحه/بستر/مرورگر از بین رفته — معمولاً بستنٔ دستیٔ پنجره توسط کاربر."""
    name = type(exc).__name__
    return any(x in name for x in ("TargetClosed", "BrowserClosed", "ContextClosed"))


def _is_transient_playwright_error(exc: BaseException, *, interaction: bool) -> bool:
    """خطاهای موقتی Playwright برای تکرار (goto یا کلیک/پر کردن وقتی سایت کند یا قطع است).

    interaction=True: برای کلیک/fill — خطاهای ناشناس را موقتی نمی‌گیریم تا حلقهٔ بی‌پایان نباشد.
    interaction=False: برای goto — خطای ناشناس را موقتی می‌گیریم (شبکهٔ عجیب).
    """
    name = type(exc).__name__
    if _is_playwright_window_closed_error(exc):
        return False
    msg = str(exc).lower()
    if name == "TimeoutError":
        # منتظر ماندن برای locator که اصلاً روی این صفحه نیست → تکرار بی‌پایان (مثلاً دکمهٔ ورود روی /auth)
        if interaction and any(
            p in msg
            for p in (
                "waiting for locator",
                "waiting for selector",
                "no element matching",
                "resolved to 0 elements",
            )
        ):
            return False
        return True
    if interaction and "strict mode violation" in msg:
        return False
    if "timeout" in msg or "timed out" in msg or ("ms" in msg and "exceeded" in msg):
        return True
    if interaction and any(
        p in msg
        for p in (
            "not attached",
            "detached",
            "not visible",
            "not stable",
            "element is not visible",
        )
    ):
        return True
    if "net::err_" in msg:
        permanent_net = (
            "net::err_invalid_url",
            "net::err_unknown_url_scheme",
            "net::err_disallowed_url_scheme",
            "net::err_name_not_resolved",
        )
        return not any(p in msg for p in permanent_net)
    if any(
        p in msg
        for p in (
            "invalid url",
            "unsupported protocol",
            "illegal url",
            "expected object, got null",
        )
    ):
        return False
    # خطای سوکت/سیستم‌عامل بدون پیشوند net:: (ویندوز، DNS، …)
    net_soft = (
        "econnreset",
        "econnrefused",
        "etimedout",
        "connection refused",
        "connection reset",
        "connection aborted",
        "connection closed",
        "connection lost",
        "host unreachable",
        "network unreachable",
        "no route to host",
        "getaddrinfo",
        "nodename nor servname",
        "name or service not known",
        "temporary failure in name resolution",
        "network is unreachable",
        "software caused connection abort",
        "broken pipe",
        "unexpected eof",
        "winerror 10060",
        "winerror 10061",
        "wsa",
        "10060",
        "10061",
    )
    if any(k in msg for k in net_soft):
        return True
    return not interaction


async def _sleep_cancellable(
    total_s: float,
    cancel_check: Callable[[], Awaitable[bool]] | None,
    *,
    slice_s: float = 0.5,
) -> None:
    """منتظر می‌ماند؛ اگر cancel_check True شود PlaybackCancelled می‌زند."""
    total_s = max(0.0, float(total_s))
    if not cancel_check:
        await asyncio.sleep(total_s)
        return

    deadline = time.monotonic() + total_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if await cancel_check():
            raise PlaybackCancelled("اجرای سناریو با توقف شما متوقف شد.")
        await asyncio.sleep(min(max(0.05, slice_s), remaining))


async def _goto_playback_once(page: Any, url: str, *, timeout_nav: float, turbo: bool = False) -> None:
    if turbo:
        # حالت تهاجمی: حداکثر ۸ ثانیه برای DOM صبر کن، وگرنه با commit جلو برو —
        # پولینگ ۵۰ms لوکیتورها فیلد را میلی‌ثانیه‌ای شکار می‌کند، نه لود کامل صفحه.
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=min(float(timeout_nav), 8000.0))
        except Exception:
            try:
                await page.goto(url, wait_until="commit", timeout=5000.0)
            except Exception:
                pass
        return
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_nav)
    try:
        await page.wait_for_load_state("load", timeout=min(45000.0, timeout_nav))
    except Exception:
        log.debug("load state پس از goto تمام نشد (عادی برای بعضی سایت‌ها).")


def _playback_url_skips_periodic_health_reload(url: str) -> bool:
    """
    یک reload کامل هر N ثانیه روی مسیرهای ورود/OTP بسیاری SPAها را قطع یا با اکشن‌ها همپوشانی می‌کند؛
    باید رد شود تا مثلاً صفحهٔ okala تا زمان دستی کردن OTP رفرش نشود.
    """
    raw = (url or "").strip()
    if not raw.lower().startswith(("http://", "https://")):
        return False
    low = raw.lower()
    try:
        p = urlparse(raw)
        pq = ((p.path or "") + ("?" + p.query if p.query else "")).lower()
    except Exception:
        pq = ""
    needles = (
        "/auth",
        "/login",
        "/signin",
        "sign-in",
        "/signup",
        "/register",
        "/otp",
        "=otp",
        "page=otp",
        "/verification",
        "/verify",
        "/recovery",
        "captcha",
    )
    blob = low + (" " + pq if pq else "")
    return any(n in blob for n in needles)


async def _playback_optional_health_reload(
    page: Any,
    *,
    interval_s: float,
    last_tick: list[float],
    goto_timeout_ms: int,
    log_lines: list[str] | None,
) -> None:
    """پس از گذشت بازه، یک بار page.reload برای امتحان وصل بودن شبکه/سایت."""
    if interval_s <= 0:
        return
    now = time.monotonic()
    if now - last_tick[0] < interval_s:
        return
    pb = get_playback_file_logger()
    try:
        cur = (page.url or "").strip()
    except Exception:
        cur = ""
    if not cur.startswith(("http://", "https://")):
        last_tick[0] = now
        return
    if _playback_url_skips_periodic_health_reload(cur):
        pb.debug(
            "$ رفرش دورهٔ بررسیٔ اتصال رد شد تا جریان ورود/OTP خراب نشود | url=%s",
            cur[:260],
        )
        last_tick[0] = now
        return
    t_nav = float(min(max(3000, goto_timeout_ms), 300_000))
    try:
        pb.info("$ رفرش بررسیٔ اتصال (~هر %ss) | url=%s", round(interval_s, 2), cur[:220])
        await page.reload(wait_until="domcontentloaded", timeout=t_nav)
        try:
            await page.wait_for_load_state("load", timeout=min(45000.0, t_nav))
        except Exception:
            pass
        if log_lines is not None:
            log_lines.append(
                "بررسی اتصال: صفحه رفرش شد"
                + (f" ({cur[:72]}{'…' if len(cur) > 72 else ''})" if cur else ""),
            )
    except BaseException as exc:
        if is_target_closed_error(exc):
            raise PlaybackSessionLost(
                "مرورگر پخش حین رفرش بررسیٔ اتصال بسته شد یا از دست رفت.",
            ) from exc
        pb.warning("$ رفرش بررسیٔ اتصال ناموفق | %s", exc)
        if log_lines is not None:
            log_lines.append(f"بررسی اتصال: رفرش ناموفق — {str(exc)[:200]}")
    finally:
        last_tick[0] = time.monotonic()


def _resolve_selector_candidates(
    selector: str | list[str] | None,
    selectors: list[str] | None = None,
) -> list[str]:
    """
    لیست اولویت‌دار selectorها:
    اگر selectors پاس داده شده باشد، به ترتیب اولویت بررسی می‌شود.
    اگر selector تکی هم پاس شده باشد و در selectors نباشد، به عنوان آخرین fallback اضافه می‌شود.
    اگر selectors خالی باشد، فقط selector تکی برمی‌گردد.
    """
    cands: list[str] = []
    if selectors and isinstance(selectors, list):
        for s in selectors:
            if isinstance(s, str) and s.strip() and s.strip() not in cands:
                cands.append(s.strip())
    elif isinstance(selector, list):
        for s in selector:
            if isinstance(s, str) and s.strip() and s.strip() not in cands:
                cands.append(s.strip())
    if isinstance(selector, str) and selector.strip() and selector.strip() not in cands:
        cands.append(selector.strip())
    return cands


def _playback_frame_locators_first(page: Any, selector: str) -> list[Any]:
    """اولین locator برای هر frame؛ بعضی ورودها داخل iframe هستند."""
    sel = selector.strip()
    if not sel:
        return []
    out: list[Any] = []
    seen: set[int] = set()
    frames: list[Any] = []
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    if not frames:
        try:
            return [page.locator(sel).first]
        except Exception:
            return []
    for fr in frames:
        try:
            fid = id(fr)
            if fid in seen:
                continue
            seen.add(fid)
            lc = fr.locator(sel).first
            out.append(lc)
        except Exception:
            continue
    return out or [page.locator(sel).first]


async def _playback_first_visible_locator(
    page: Any,
    selector: str,
    *,
    timeout_ms: int,
    turbo: bool = False,
    selectors: list[str] | None = None,
) -> Any:
    """اولین locator واقعاً دیده‌شونده در هر frame (نه نسخهٔ مخفیٔ تکراری). پشتیبانی از لیست fallback بدون معطلی سریالی."""
    cands = _resolve_selector_candidates(selector, selectors)
    if not cands:
        raise RuntimeError("selector خالی")

    deadline = time.monotonic() + max(0.35, float(timeout_ms) / 1000.0)
    poll_sleep = 0.02 if turbo else 0.05
    last_exc: BaseException | None = None

    while time.monotonic() < deadline:
        for sel in cands:
            for loc in _playback_frame_locators_first(page, sel):
                try:
                    if await loc.is_visible():
                        return loc
                except BaseException as exc:
                    last_exc = exc
        await asyncio.sleep(poll_sleep)

    remain_ms = int(max(200, (deadline - time.monotonic()) * 1000))
    for sel in cands:
        loc = page.locator(sel).first
        try:
            if await loc.is_visible():
                return loc
            await loc.wait_for(state="visible", timeout=min(remain_ms, 400))
            return loc
        except BaseException as exc:
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"هیچ‌یک از selectorها دیده نشد: {cands}")


async def _playback_wait_auth_form_field(
    page: Any,
    selector: str,
    *,
    timeout_ms: int,
    turbo: bool = False,
    selectors: list[str] | None = None,
) -> None:
    """روی مسیر ورود/OTP تا دیده شدن فیلد (مثلاً #mobile) صبر می‌کند."""
    if turbo:
        return
    cands = _resolve_selector_candidates(selector, selectors)
    if not cands:
        return
    try:
        url = (page.url or "").strip()
    except Exception:
        return
    if not _playback_url_skips_periodic_health_reload(url):
        return
    probe_ms = min(max(int(timeout_ms), 3000), 45_000)
    pb = get_playback_file_logger()
    try:
        await _playback_first_visible_locator(page, cands[0], timeout_ms=probe_ms, selectors=cands)
        pb.debug("فیلد ورود/OTP آماده | selector=%r url=%s", cands[0][:80], url[:140])
    except BaseException as exc:
        pb.warning(
            "فیلد ورود هنوز دیده نشد (fill باز هم امتحان می‌شود) | selector=%r | %s",
            cands[0][:80],
            exc,
        )


async def _playback_should_skip_redundant_click(
    page: Any,
    selector: str,
    steps: list[dict[str, Any]],
    step_index: int,
    values: dict[str, Any],
) -> bool:
    """
    وقتی کاربر/قدم قبلی قبلاً به صفحهٔ auth رسیده و دکمهٔ «ورود» دیگر نیست
    ولی فیلد موبایل (قدم fill بعدی) آماده است — کلیک را رد کن تا روی fill برسیم.
    """
    sel = selector.strip()
    if not sel:
        return False
    try:
        url = (page.url or "").strip()
    except Exception:
        return False
    if not _playback_url_skips_periodic_health_reload(url):
        return False
    try:
        if await page.locator(sel).first.is_visible(timeout=PLAYBACK_SKIP_CLICK_PROBE_MS):
            return False
    except BaseException:
        pass

    pb = get_playback_file_logger()
    for j in range(step_index + 1, min(step_index + 6, len(steps))):
        nxt = steps[j]
        ty = (nxt.get("type") or "").lower()
        if ty == "fill":
            fs = (nxt.get("selector") or "").strip()
            f_selectors = nxt.get("selectors") if isinstance(nxt.get("selectors"), list) else None
            if not fs and not f_selectors:
                continue
            try:
                loc = await _playback_first_visible_locator(
                    page,
                    fs,
                    timeout_ms=min(10_000, PLAYBACK_SKIP_CLICK_PROBE_MS + 5000),
                    selectors=f_selectors,
                )
                await loc.scroll_into_view_if_needed(timeout=8000)
                pb.info(
                    "$ کلیک رد شد: فرم ورود آماده و فیلد بعدی دیده می‌شود | skip_click=%r next_fill=%r | url=%s",
                    sel[:72],
                    (fs or (f_selectors[0] if f_selectors else ""))[:72],
                    url[:140],
                )
                return True
            except BaseException:
                continue
        if ty == "goto":
            target = _playback_resolve(str(nxt.get("url") or ""), values).strip()
            if not target:
                continue
            try:
                cur = (page.url or "").strip()
            except Exception:
                cur = ""
            if cur and (_urls_equivalent(cur, target) or _playback_url_skips_periodic_health_reload(cur)):
                pb.info(
                    "$ کلیک رد شد: نشانی فعلی با goto بعدی هم‌خوان است | skip_click=%r goto=%r | url=%s",
                    sel[:72],
                    target[:72],
                    url[:140],
                )
                return True
    return False


def _playback_upcoming_sms_otp_fill(
    steps: list[dict[str, Any]],
    step_index: int,
) -> dict[str, Any] | None:
    for j in range(step_index + 1, min(step_index + 6, len(steps))):
        nxt = steps[j]
        if (nxt.get("type") or "").lower() != "fill":
            continue
        if nxt.get("sms_otp") or nxt.get("sms_otp_fill"):
            return nxt
        sel = (nxt.get("selector") or "").lower()
        if "otp" in sel or "login-otp" in sel:
            return nxt
    return None


def _playback_should_skip_destructive_goto(
    page: Any,
    target_url: str,
    steps: list[dict[str, Any]],
    step_index: int,
) -> bool:
    """goto به منوی اصلی/فروشگاه وسط جریان OTP — صفحهٔ کد را از بین می‌برد (مثلاً okala/stores)."""
    target = target_url.strip()
    if not target:
        return False
    try:
        cur = (page.url or "").strip()
    except Exception:
        return False
    if not _playback_url_skips_periodic_health_reload(cur):
        return False
    if _playback_url_skips_periodic_health_reload(target):
        return False
    if _playback_upcoming_sms_otp_fill(steps, step_index) is not None:
        get_playback_file_logger().info(
            "$ goto رد شد: خروج از ورود/OTP در حالی که قدم بعدی کد پیامک است | target=%r | url=%s",
            target[:100],
            cur[:140],
        )
        return True
    return False


async def _playback_should_skip_redundant_auth_goto(page: Any, target_url: str) -> bool:
    """اگر SPA قبلاً روی همان مسیر OTP است، goto تکراری باعث رفرش/برگشت می‌شود."""
    target = target_url.strip()
    if not target:
        return False
    try:
        cur = (page.url or "").strip()
    except Exception:
        return False
    if not _playback_url_skips_periodic_health_reload(cur):
        return False
    if _urls_equivalent(cur, target):
        return True
    low_t = target.lower()
    low_c = cur.lower()
    if "otp" in low_t and ("page=otp" in low_c or "otp" in low_c.split("?")[-1]):
        get_playback_file_logger().info(
            "$ goto رد شد: صفحهٔ OTP از قبل باز است | target=%r | url=%s",
            target[:100],
            cur[:140],
        )
        return True
    return False


async def _playback_wait_until_otp_screen(
    page: Any,
    *,
    otp_selector: str,
    timeout_ms: int,
    cancel_check: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    """پس از زدن «ارسال کد» تا دیده شدن فیلد OTP یا ?page=otp صبر می‌کند."""
    sel = (otp_selector or "#login-otp").strip()
    deadline = time.monotonic() + max(8.0, float(timeout_ms) / 1000.0)
    pb = get_playback_file_logger()
    pb.info("$ منتظر صفحه/فیلد OTP… selector=%r", sel[:72])
    while time.monotonic() < deadline:
        if cancel_check and await cancel_check():
            raise PlaybackCancelled("توقف هنگام انتظار برای صفحهٔ OTP.")
        try:
            url = (page.url or "").lower()
        except Exception:
            url = ""
        if "page=otp" in url or ("otp" in url and "/auth" in url):
            pb.info("$ صفحهٔ OTP (نشانی) | %s", url[:160])
            break
        try:
            if await page.locator(sel).first.is_visible(timeout=1800):
                pb.info("$ فیلد OTP دیده شد | url=%s", url[:160])
                break
        except BaseException:
            pass
        await asyncio.sleep(0.28)
    remain_ms = int(max(3000, (deadline - time.monotonic()) * 1000))
    await _playback_wait_auth_form_field(page, sel, timeout_ms=remain_ms)


async def _goto_playback_resilient(
    page: Any,
    url: str,
    *,
    timeout_nav: float,
    max_attempts: int | None,
    pause_between_attempts_s: float,
    cancel_check: Callable[[], Awaitable[bool]] | None = None,
    progress_log: list[str] | None = None,
    turbo: bool = False,
) -> int:
    last_exc: BaseException | None = None
    attempt = 0
    # رعایت سقف‌های موجود؛ در صورت نامشخص بودن، اعمال سقف ایمنی برای جلوگیری از حلقه بی‌پایان روی URL مرده
    effective_max = max_attempts if max_attempts is not None else DEFAULT_NAV_RETRY_DEAD_URL_CAP
    while attempt < effective_max:
        attempt += 1
        if cancel_check and await cancel_check():
            raise PlaybackCancelled("اجرای سناریو با توقف شما متوقف شد.")
        try:
            await _goto_playback_once(page, url, timeout_nav=timeout_nav, turbo=turbo)
            if attempt > 1:
                log.info("goto موفق پس از %s تلاش برای %s", attempt, url[:80])
            return attempt
        except PlaybackCancelled:
            raise
        except BaseException as exc:
            last_exc = exc
            trans = _is_transient_playwright_error(exc, interaction=False)
            at_limit = attempt >= effective_max
            cap = str(max_attempts) if max_attempts is not None else f"{effective_max} (سقف خودکار)"
            if at_limit or not trans:
                get_playback_file_logger().error(
                    "goto قطع تکرار (شکست نهایی یا غیرموقت) | attempt=%s/%s transient=%s at_limit=%s | url=%r | err=%s",
                    attempt,
                    cap,
                    trans,
                    at_limit,
                    url[:300],
                    repr(exc),
                )
                raise
            log.warning(
                "باز کردن صفحه تلاش %s/%s ناموفق؛ %ss بعد دوباره — %s",
                attempt,
                cap,
                pause_between_attempts_s,
                exc,
            )
            get_playback_file_logger().info(
                "goto تلاش مجدد شبکه | attempt=%s/%s transient=True timeout_nav=%sms url=%r | جزئیات=%s",
                attempt,
                cap,
                timeout_nav,
                url[:280],
                repr(exc),
            )
            if progress_log is not None:
                progress_log.append(
                    f"بار {attempt}: صفحه باز نشد (شبکه قطع یا تأخیر)؛ {pause_between_attempts_s:g} ثانیه صبر و دوباره…",
                )
            await _sleep_cancellable(pause_between_attempts_s, cancel_check)

    assert last_exc is not None
    get_playback_file_logger().error("goto تمام بدون بازگشت طبیعی؛ آخرین خطا: %s", repr(last_exc))
    raise last_exc


def _coerce_bool(raw: Any, default: bool = False) -> bool:
    """تبدیل انواع ورودی (bool, str, int) به مقدار بولی."""
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
    return bool(raw)


def _resolve_turbo_params(
    scenario: dict[str, Any] | None,
    step: dict[str, Any] | None = None,
    default_turbo: bool | None = None,
    default_interval: int | None = None,
    default_keep: bool | None = None,
) -> tuple[bool, int, bool]:
    """
    استخراج و تنظیم پارامترهای سناریو برای حالت توربو:
    (turbo_refresh, refresh_interval_ms, keep_open_on_missing)
    """
    sc = scenario or {}
    st = step or {}

    # فقط کلید صریح turbo_refresh حلقه رفرش (reload) را فعال می‌کند؛
    # turbo_mode/turbo یعنی «سرعت بدون مکث» ولی بدون رفرش صفحه.
    raw_turbo = None
    if "turbo_refresh" in st:
        raw_turbo = st.get("turbo_refresh")
    elif "turbo_refresh" in sc:
        raw_turbo = sc.get("turbo_refresh")

    if raw_turbo is not None:
        turbo_refresh = _coerce_bool(raw_turbo, default=False)
    elif default_turbo is not None:
        turbo_refresh = bool(default_turbo)
    else:
        turbo_refresh = False

    raw_interval = st.get("refresh_interval_ms")
    if raw_interval is None:
        raw_interval = sc.get("refresh_interval_ms")

    if raw_interval is not None and raw_interval != "":
        try:
            refresh_interval_ms = int(float(raw_interval))
        except Exception:
            refresh_interval_ms = default_interval if default_interval is not None else (80 if turbo_refresh else 1000)
    else:
        refresh_interval_ms = default_interval if default_interval is not None else (80 if turbo_refresh else 1000)
    refresh_interval_ms = max(10, min(refresh_interval_ms, 60_000))

    raw_keep = st.get("keep_open_on_missing")
    if raw_keep is None:
        raw_keep = sc.get("keep_open_on_missing")

    def_keep = default_keep if default_keep is not None else (True if turbo_refresh else False)
    keep_open_on_missing = _coerce_bool(raw_keep, default=def_keep)

    return turbo_refresh, refresh_interval_ms, keep_open_on_missing


def _is_timeout_error(exc: BaseException) -> bool:
    """آیا این خطا مربوط به تایم‌اوت یا باز نشدن/پیدا نشدن المان است؟"""
    name = type(exc).__name__
    if "Timeout" in name:
        return True
    msg = str(exc).lower()
    return any(
        p in msg
        for p in (
            "timeout",
            "timed out",
            "exceeded",
            "waiting for locator",
            "waiting for selector",
            "no element matching",
            "resolved to 0 elements",
            "element is not visible",
        )
    )


async def _turbo_wait_and_refresh(
    page: Any,
    selector: str,
    timeout: float = 1800.0,
    *,
    refresh_interval_ms: int = 80,
    reload_every_s: float = 2.0,
    cancel_check: Callable[[], Awaitable[bool]] | None = None,
    progress_log: list[str] | None = None,
    selectors: list[str] | None = None,
) -> Any:
    """
    حلقهٔ تکرار و رفرش تهاجمی صفحه تا زمانی که المان باز شود (Turbo Mode).
    هرگز تسک را به خاطر باز نشدن فیلد نمی‌بندد و بلافاصله با کمترین تأخیر رفرش و امتحان می‌کند.
    """
    cands = _resolve_selector_candidates(selector, selectors)
    if not cands:
        raise RuntimeError("selector خالی برای turbo_wait_and_refresh")
    sel = cands[0]

    deadline = time.monotonic() + max(1.0, float(timeout if timeout is not None else 1800.0))
    refreshes = 0
    pb = get_playback_file_logger()
    pb.info(
        "🚀 شروع رفرش تهاجمی توربو | المان: %r | بازه: %sms | مهلت کل: %ss",
        sel,
        refresh_interval_ms,
        timeout,
    )
    if progress_log is not None:
        progress_log.append(f"🚀 حالت توربو فعال شد: رفرش تهاجمی تا باز شدن فیلد/دکمه {sel[:40]}…")

    wait_ms = max(10, int(refresh_interval_ms))
    # Poll visibility aggressively (50ms = centisecond reaction), but reload rarely:
    # constant reloads reset page JS timers (self-defeating) and kill the session.
    total_s = max(1.0, float(timeout if timeout is not None else 1800.0))
    grace_s = min(10.0, max(2.0, total_s * 0.25))
    reload_every_s = max(0.5, float(reload_every_s or 2.0))
    poll_s = 0.05
    next_reload_at = time.monotonic() + grace_s
    last_url = ""
    try:
        last_url = (page.url or "").strip()
    except Exception:
        last_url = ""

    while True:
        if cancel_check and await cancel_check():
            raise PlaybackCancelled("اجرای سناریو با توقف شما متوقف شد.")
        if page.is_closed():
            raise PlaybackSessionLost("صفحه یا مرورگر پخش بسته شده است.")
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"مهلت زمانی توربو ({timeout}s) برای دیده شدن المان '{sel}' به پایان رسید."
            )

        # اگر آدرس عوض شده (ریدایرکت OAuth/SPA در جریان است): رفرش ممنوع —
        # رفرش وسط ریدایرکت آن را ریست و حلقه خودتخریب می‌سازد. فقط پول منفعل.
        try:
            cur_url = (page.url or "").strip()
        except Exception:
            cur_url = last_url
        if cur_url != last_url:
            last_url = cur_url
            next_reload_at = time.monotonic() + reload_every_s

        # ۱) بررسی بدون درنگ (no sleep) برای مشاهده المان
        for s in cands:
            for cand in _playback_frame_locators_first(page, s):
                try:
                    if await cand.is_visible():
                        if refreshes > 0:
                            pb.info("✅ المان %r پس از %d رفرش ظاهر شد!", s, refreshes)
                            if progress_log is not None:
                                progress_log.append(
                                    f"✅ المان {s[:40]} پس از {refreshes} رفرش باز شد؛ اجرای آنی!"
                                )
                        return cand
                except PlaybackCancelled:
                    raise
                except Exception:
                    pass

        # ۲) فاز انتظار منفعل (بدون رفرش): برای المان‌های دیررندر JS —
        # رفرش در این فاز تایمرهای صفحه را ریست و شرط را نابود می‌کند.
        if time.monotonic() < next_reload_at:
            await asyncio.sleep(poll_s)
            continue

        # ۳) فاز رفرش دوره‌ای (حداکثر هر چند ثانیه): برای صفحات «ظرفیت/نوبت» که
        # فقط با رفرش شانس جدید می‌دهند.
        refreshes += 1
        if refreshes == 1 or refreshes % 10 == 0:
            pb.info("🔄 توربو رفرش #%d برای المان %r", refreshes, sel)
            if progress_log is not None:
                progress_log.append(f"🔄 توربو رفرش #{refreshes}: منتظر باز شدن المان…")

        try:
            await page.reload(wait_until="commit", timeout=3000)
        except PlaybackCancelled:
            raise
        except Exception as reload_err:
            pb.debug("خطای موقت در reload توربو: %s", reload_err)

        if cancel_check and await cancel_check():
            raise PlaybackCancelled("اجرای سناریو با توقف شما متوقف شد.")
        if page.is_closed():
            raise PlaybackSessionLost("صفحه یا مرورگر پخش بسته شده است.")
        next_reload_at = time.monotonic() + reload_every_s

        # ۴) انتظار سریع locator تا سقف refresh_interval_ms (اگر المان سریع ظاهر شد بدون معطلی برگردد)
        for s in cands:
            try:
                loc = page.locator(s).first
                await loc.wait_for(state="visible", timeout=wait_ms)
                pb.info("✅ المان %r بلافاصله پس از رفرش #%d ظاهر شد!", s, refreshes)
                if progress_log is not None:
                    progress_log.append(
                        f"✅ المان {s[:40]} پس از {refreshes} رفرش باز شد؛ اجرای آنی!"
                    )
                return loc
            except PlaybackCancelled:
                raise
            except Exception:
                pass


async def _playback_click(
    page: Any,
    selector: str,
    *,
    timeout_ms: int,
    show_marker: bool = True,
    turbo: bool = False,
    turbo_refresh: bool = False,
    refresh_interval_ms: int = 80,
    turbo_timeout_s: float = 1800.0,
    cancel_check: Callable[[], Awaitable[bool]] | None = None,
    progress_log: list[str] | None = None,
    selectors: list[str] | None = None,
) -> None:
    cands = _resolve_selector_candidates(selector, selectors)
    if not cands:
        raise RuntimeError("خالی بودن selector برای کلیک")
    sel = cands[0]
    is_tb = turbo or turbo_refresh
    last_exc: BaseException | None = None
    retries = 0 if is_tb else PLAYBACK_CLICK_RETRIES
    eff_timeout = min(timeout_ms, max(80, int(refresh_interval_ms))) if turbo_refresh else timeout_ms
    for attempt in range(retries + 1):
        if cancel_check and await cancel_check():
            raise PlaybackCancelled("اجرای سناریو با توقف شما متوقف شد.")
        try:
            try:
                loc = await _playback_first_visible_locator(page, sel, timeout_ms=eff_timeout, turbo=is_tb, selectors=selectors)
            except BaseException as exc_loc:
                if turbo_refresh and _is_timeout_error(exc_loc):
                    loc = await _turbo_wait_and_refresh(
                        page,
                        sel,
                        timeout=turbo_timeout_s,
                        refresh_interval_ms=refresh_interval_ms,
                        cancel_check=cancel_check,
                        progress_log=progress_log,
                        selectors=selectors,
                    )
                else:
                    raise
            if not is_tb:
                await loc.scroll_into_view_if_needed(timeout=timeout_ms)
                if show_marker:
                    await _show_action_marker(page, loc, mode="click", timeout_ms=timeout_ms)
            else:
                try:
                    if not await loc.is_visible():
                        await loc.scroll_into_view_if_needed(timeout=min(timeout_ms, 1500))
                except BaseException:
                    pass
            try:
                await loc.click(timeout=timeout_ms)
            except BaseException:
                await loc.click(timeout=min(timeout_ms, 3000), force=True)
            if show_marker and not is_tb:
                await _hide_action_marker(page, 550)
            return
        except PlaybackCancelled:
            raise
        except BaseException as exc:
            if show_marker and not is_tb:
                await _hide_action_marker(page, 0)
            last_exc = exc
            if turbo_refresh and _is_timeout_error(exc) and "مهلت زمانی توربو" not in str(exc):
                try:
                    loc = await _turbo_wait_and_refresh(
                        page,
                        sel,
                        timeout=turbo_timeout_s,
                        refresh_interval_ms=refresh_interval_ms,
                        cancel_check=cancel_check,
                        progress_log=progress_log,
                        selectors=selectors,
                    )
                    try:
                        await loc.click(timeout=timeout_ms)
                    except BaseException:
                        await loc.click(timeout=min(timeout_ms, 3000), force=True)
                    return
                except PlaybackCancelled:
                    raise
                except BaseException as turbo_exc:
                    last_exc = turbo_exc
        if attempt >= retries:
            break
        log.warning(
            "کلیک تلاش %s/%s شکست (دوباره پس از %ss): %s",
            attempt + 1,
            retries + 1,
            PLAYBACK_RETRY_PAUSE_S,
            last_exc,
        )
        await asyncio.sleep(PLAYBACK_RETRY_PAUSE_S)
    assert last_exc is not None
    _reraise_click_fill_session(last_exc)


async def _playback_click_resilient(
    page: Any,
    selector: str,
    *,
    timeout_ms: int,
    show_marker: bool = True,
    max_attempts: int | None,
    pause_between_attempts_s: float,
    cancel_check: Callable[[], Awaitable[bool]] | None,
    progress_log: list[str] | None,
    action_label: str,
    turbo: bool = False,
    turbo_refresh: bool = False,
    refresh_interval_ms: int = 80,
    turbo_timeout_s: float = 1800.0,
    selectors: list[str] | None = None,
) -> int:
    """تلاش چندمرحله‌ای برای کلیک وقتی سایت کند است (همان سقف/فاصلهٔ nav_retry_*)."""
    last_exc: BaseException | None = None
    outer = 0
    is_tb = turbo or turbo_refresh
    if is_tb:
        pause_between_attempts_s = min(float(pause_between_attempts_s), 0.5)
    _tb_start = time.monotonic()
    while max_attempts is None or outer < max_attempts or turbo_refresh:
        outer += 1
        if cancel_check and await cancel_check():
            raise PlaybackCancelled("اجرای سناریو با توقف شما متوقف شد.")
        try:
            await _playback_click(
                page,
                selector,
                timeout_ms=timeout_ms,
                show_marker=show_marker if not is_tb else False,
                turbo=is_tb,
                turbo_refresh=turbo_refresh,
                refresh_interval_ms=refresh_interval_ms,
                turbo_timeout_s=turbo_timeout_s,
                cancel_check=cancel_check,
                progress_log=progress_log,
                selectors=selectors,
            )
            return outer
        except PlaybackCancelled:
            raise
        except BaseException as exc:
            last_exc = exc
            if isinstance(exc, PlaybackSessionLost) or _is_playwright_window_closed_error(exc):
                # مرورگر/صفحه واقعاً بسته شده — تلاش مجدد بی‌فایده و مساوی هنگ است.
                _reraise_click_fill_session(exc)
            if turbo_refresh and _is_timeout_error(exc):
                if time.monotonic() - _tb_start >= max(1.0, float(turbo_timeout_s or 1800.0)):
                    # سقف زمانی توربو تمام شد — دیگر رفرش نکن، خطا را برگردان تا تسک تمیز ببندد.
                    _reraise_click_fill_session(exc)
                get_playback_file_logger().info(
                    "توربو کلیک: تایم‌اوت المان؛ رفرش و تلاش مجدد | sel=%r | exc=%s",
                    selector[:80],
                    exc,
                )
                try:
                    await _turbo_wait_and_refresh(
                        page,
                        selector,
                        timeout=turbo_timeout_s,
                        refresh_interval_ms=refresh_interval_ms,
                        cancel_check=cancel_check,
                        progress_log=progress_log,
                        selectors=selectors,
                    )
                    continue
                except PlaybackCancelled:
                    raise
                except BaseException as t_exc:
                    last_exc = t_exc

            trans = _is_transient_playwright_error(exc, interaction=True)
            at_limit = (max_attempts is not None and outer >= max_attempts) and not turbo_refresh
            cap = "∞" if (max_attempts is None or turbo_refresh) else str(max_attempts)
            if at_limit or (not trans and not turbo_refresh):
                get_playback_file_logger().error(
                    "کلیک قطع تکرار | outer=%s/%s transient=%s at_limit=%s | label=%r sel=%r | %s",
                    outer,
                    cap,
                    trans,
                    at_limit,
                    action_label,
                    selector[:220],
                    repr(exc),
                )
                _reraise_click_fill_session(exc)
            log.warning(
                "کلیک %s تلاش بیرونی %s/%s ناموفق؛ %ss بعد دوباره — %s",
                action_label,
                outer,
                cap,
                pause_between_attempts_s,
                exc,
            )
            get_playback_file_logger().info(
                "کلیک تلاش مجدد | outer=%s/%s | %r | %s",
                outer,
                cap,
                action_label,
                repr(exc),
            )
            if progress_log is not None:
                progress_log.append(
                    f"بار {outer} ({action_label}): المان یا صفحه آماده نشد؛ "
                    f"{pause_between_attempts_s:g} ثانیه صبر و دوباره…",
                )
            await _sleep_cancellable(pause_between_attempts_s, cancel_check)
    assert last_exc is not None
    get_playback_file_logger().error("کلیک شکست کامل؛ آخرین خطا=%s selector=%r", repr(last_exc), selector[:240])
    _reraise_click_fill_session(last_exc)


async def _playback_fill_interact(
    loc: Any,
    fill_v: str,
    *,
    timeout_ms: int,
    turbo: bool = False,
    type_text: bool = False,
    char_delay_ms: int = 0,
    press_enter: bool = False,
) -> None:
    """
    یک بار تعامل با فیلد: fill استاندارد یا تایپ کاراکتر به کاراکتر، سپس ست کردن مقدار با setter بومی (React/Angular) + رویدادهای input و keyup.
    """
    t = min(max(int(timeout_ms), 500), 120_000)
    want = str(fill_v)

    # فرم‌هایی مثل ایزی‌تریدر فیلد readonly + کیبورد مجازی (uikeyboard) دارند؛
    # fill/tap استاندارد روی readonly خطا می‌دهد، پس اول readonly را برمی‌داریم.
    try:
        await loc.evaluate("(el) => { if (el) el.removeAttribute('readonly'); }")
    except BaseException:
        pass

    if type_text or char_delay_ms > 0:
        delay = char_delay_ms if char_delay_ms > 0 else 40
        try:
            await loc.click(timeout=min(t, 2000))
        except BaseException:
            pass
        try:
            await loc.fill("", timeout=min(t, 1500))
        except BaseException:
            try:
                await loc.press("Control+A")
                await loc.press("Backspace")
            except BaseException:
                pass
        if hasattr(loc, "press_sequentially"):
            await loc.press_sequentially(want, delay=delay, timeout=t)
        else:
            await loc.fill(want, timeout=t)
        if press_enter:
            try:
                await loc.press("Enter")
            except BaseException:
                pass
        try:
            await loc.evaluate(
                """(el) => {
              if (!el) return;
              el.dispatchEvent(new Event("input", { bubbles: true }));
              el.dispatchEvent(new Event("change", { bubbles: true }));
              el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true }));
            }"""
            )
        except BaseException:
            pass
        return

    if turbo:
        try:
            await loc.fill(want, timeout=min(t, 1500))
            return
        except BaseException:
            pass
        try:
            if hasattr(loc, "press_sequentially"):
                await loc.press_sequentially(want, delay=0, timeout=min(t, 1500))
                return
        except BaseException:
            pass
        await loc.evaluate(
            """(el, val) => {
          if (!el || val === undefined || val === null) return;
          const v = String(val);
          const proto =
            el.tagName === "TEXTAREA"
              ? window.HTMLTextAreaElement.prototype
              : window.HTMLInputElement.prototype;
          const d = Object.getOwnPropertyDescriptor(proto, "value");
          if (d && d.set) d.set.call(el, v);
          else el.value = v;
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
          el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true }));
          try {
            el.dispatchEvent(new InputEvent("input", { bubbles: true, data: v, inputType: "insertFromPaste" }));
          } catch (e) {}
        }""",
            want,
        )
        return

    err_fill: BaseException | None = None
    try:
        await loc.click(timeout=min(t, 12_000))
    except BaseException:
        pass
    try:
        await loc.fill(want, timeout=t)
        try:
            got = await loc.input_value()
            if got.strip() == want.strip():
                return
        except BaseException:
            pass
    except BaseException as exc:
        err_fill = exc

    try:
        await loc.click(timeout=min(t, 20_000))
    except BaseException:
        pass
    try:
        await loc.fill("", timeout=min(8000, t))
    except BaseException:
        try:
            await loc.press("Control+A")
            await loc.press("Backspace")
        except BaseException:
            pass
    err_seq: BaseException | None = None
    try:
        if hasattr(loc, "press_sequentially"):
            await loc.press_sequentially(want, delay=10, timeout=t)
        else:
            raise AttributeError("press_sequentially")
        try:
            got2 = await loc.input_value()
            if got2.strip() == want.strip():
                return
        except BaseException:
            pass
    except BaseException as exc_seq:
        err_seq = exc_seq
    try:
        await loc.evaluate(
            """(el, val) => {
          if (!el || val === undefined || val === null) return;
          const v = String(val);
          const proto =
            el.tagName === "TEXTAREA"
              ? window.HTMLTextAreaElement.prototype
              : window.HTMLInputElement.prototype;
          const d = Object.getOwnPropertyDescriptor(proto, "value");
          if (d && d.set) d.set.call(el, v);
          else el.value = v;
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
          el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true }));
          try {
            el.dispatchEvent(new InputEvent("input", { bubbles: true, data: v, inputType: "insertFromPaste" }));
          } catch (e) {}
        }""",
            want,
        )
        return
    except BaseException as exc_ev:
        raise RuntimeError(
            f"پر کردن فیلد (همهٔ روش‌ها) ناموفق: fill={err_fill!r}; "
            f"sequential={err_seq!r}; evaluate={exc_ev!r}"
        ) from exc_ev


async def _playback_fill_resilient(
    page: Any,
    sel: str,
    fill_v: str,
    *,
    timeout_ms: int,
    show_marker: bool = True,
    max_attempts: int | None,
    pause_between_attempts_s: float,
    cancel_check: Callable[[], Awaitable[bool]] | None,
    progress_log: list[str] | None,
    action_label: str,
    turbo: bool = False,
    turbo_refresh: bool = False,
    refresh_interval_ms: int = 80,
    turbo_timeout_s: float = 1800.0,
    selectors: list[str] | None = None,
    type_text: bool = False,
    char_delay_ms: int = 0,
    press_enter: bool = False,
) -> int:
    last_exc: BaseException | None = None
    outer = 0
    cands = _resolve_selector_candidates(sel, selectors)
    if not cands:
        raise RuntimeError("selector خالی برای fill")
    stripped = cands[0]
    is_tb = turbo or turbo_refresh
    if is_tb:
        pause_between_attempts_s = min(float(pause_between_attempts_s), 0.5)
    _tb_start = time.monotonic()
    retries = 0 if is_tb else PLAYBACK_CLICK_RETRIES
    eff_timeout = min(timeout_ms, max(80, int(refresh_interval_ms))) if turbo_refresh else timeout_ms
    while max_attempts is None or outer < max_attempts or turbo_refresh:
        outer += 1
        if cancel_check and await cancel_check():
            raise PlaybackCancelled("اجرای سناریو با توقف شما متوقف شد.")
        try:
            for attempt in range(retries + 1):
                if cancel_check and await cancel_check():
                    raise PlaybackCancelled("اجرای سناریو با توقف شما متوقف شد.")
                round_exc: BaseException | None = None
                try:
                    try:
                        loc = await _playback_first_visible_locator(
                            page,
                            stripped,
                            timeout_ms=eff_timeout,
                            turbo=is_tb,
                            selectors=selectors,
                        )
                    except BaseException as exc_loc:
                        if turbo_refresh and _is_timeout_error(exc_loc):
                            loc = await _turbo_wait_and_refresh(
                                page,
                                stripped,
                                timeout=turbo_timeout_s,
                                refresh_interval_ms=refresh_interval_ms,
                                cancel_check=cancel_check,
                                progress_log=progress_log,
                                selectors=selectors,
                            )
                        else:
                            raise
                    if not is_tb:
                        await loc.scroll_into_view_if_needed(timeout=timeout_ms)
                        if show_marker:
                            await _show_action_marker(page, loc, mode="fill", timeout_ms=timeout_ms)
                    else:
                        try:
                            if not await loc.is_visible():
                                await loc.scroll_into_view_if_needed(timeout=min(timeout_ms, 1500))
                        except BaseException:
                            pass
                    await _playback_fill_interact(
                        loc,
                        fill_v,
                        timeout_ms=timeout_ms,
                        turbo=is_tb,
                        type_text=type_text,
                        char_delay_ms=char_delay_ms,
                        press_enter=press_enter,
                    )
                    if show_marker and not is_tb:
                        await _hide_action_marker(page, 620)
                    return outer
                except PlaybackCancelled:
                    raise
                except BaseException as exc_inner:
                    if show_marker and not is_tb:
                        await _hide_action_marker(page, 0)
                    round_exc = exc_inner
                    if turbo_refresh and _is_timeout_error(exc_inner) and "مهلت زمانی توربو" not in str(exc_inner):
                        try:
                            loc = await _turbo_wait_and_refresh(
                                page,
                                stripped,
                                timeout=turbo_timeout_s,
                                refresh_interval_ms=refresh_interval_ms,
                                cancel_check=cancel_check,
                                progress_log=progress_log,
                                selectors=selectors,
                            )
                            await _playback_fill_interact(
                        loc,
                        fill_v,
                        timeout_ms=timeout_ms,
                        turbo=is_tb,
                        type_text=type_text,
                        char_delay_ms=char_delay_ms,
                        press_enter=press_enter,
                    )
                            return outer
                        except PlaybackCancelled:
                            raise
                        except BaseException as turbo_err:
                            round_exc = turbo_err
                if round_exc is None:
                    round_exc = RuntimeError("فریمی برای این selector پیدا نشد.")
                if attempt >= retries:
                    raise round_exc
                log.warning(
                    "پر کردن فیلد تلاش %s/%s شکست (دوباره پس از %ss): %s",
                    attempt + 1,
                    retries + 1,
                    PLAYBACK_RETRY_PAUSE_S,
                    round_exc,
                )
                await asyncio.sleep(PLAYBACK_RETRY_PAUSE_S)
        except PlaybackCancelled:
            raise
        except BaseException as exc:
            last_exc = exc
            if isinstance(exc, PlaybackSessionLost) or _is_playwright_window_closed_error(exc):
                # مرورگر/صفحه واقعاً بسته شده — تلاش مجدد بی‌فایده و مساوی هنگ است.
                _reraise_click_fill_session(exc)
            if turbo_refresh and _is_timeout_error(exc):
                if time.monotonic() - _tb_start >= max(1.0, float(turbo_timeout_s or 1800.0)):
                    # سقف زمانی توربو تمام شد — دیگر رفرش نکن، خطا را برگردان تا تسک تمیز ببندد.
                    _reraise_click_fill_session(exc)
                get_playback_file_logger().info(
                    "توربو fill: تایم‌اوت فیلد؛ رفرش و تلاش مجدد | sel=%r | exc=%s",
                    stripped[:80],
                    exc,
                )
                try:
                    await _turbo_wait_and_refresh(
                        page,
                        stripped,
                        timeout=turbo_timeout_s,
                        refresh_interval_ms=refresh_interval_ms,
                        cancel_check=cancel_check,
                        progress_log=progress_log,
                        selectors=selectors,
                    )
                    continue
                except PlaybackCancelled:
                    raise
                except BaseException as t_exc:
                    last_exc = t_exc

            trans = _is_transient_playwright_error(exc, interaction=True)
            at_limit = (max_attempts is not None and outer >= max_attempts) and not turbo_refresh
            cap = "∞" if (max_attempts is None or turbo_refresh) else str(max_attempts)
            if at_limit or (not trans and not turbo_refresh):
                get_playback_file_logger().error(
                    "پُرکردن قطع تکرار | outer=%s/%s transient=%s | label=%r sel=%r | %s",
                    outer,
                    cap,
                    trans,
                    action_label,
                    sel.strip()[:220],
                    repr(exc),
                )
                _reraise_click_fill_session(exc)
            log.warning(
                "پُرکردن %s تلاش بیرونی %s/%s ناموفق؛ %ss بعد دوباره — %s",
                action_label,
                outer,
                cap,
                pause_between_attempts_s,
                exc,
            )
            get_playback_file_logger().info(
                "fill تلاش مجدد | outer=%s/%s | %r | %s",
                outer,
                cap,
                action_label,
                repr(exc),
            )
            if progress_log is not None:
                progress_log.append(
                    f"بار {outer} ({action_label}): فیلد آماده نشد؛ "
                    f"{pause_between_attempts_s:g} ثانیه صبر و دوباره…",
                )
            await _sleep_cancellable(pause_between_attempts_s, cancel_check)
    assert last_exc is not None
    get_playback_file_logger().error("پُرکردن شکست کامل؛ آخرین خطا=%s sel=%r", repr(last_exc), sel.strip()[:240])
    _reraise_click_fill_session(last_exc)


def _playback_alive_pages(context: Any) -> list[Any]:
    try:
        return [p for p in context.pages if not p.is_closed()]
    except Exception:
        return []


async def _playback_adopt_new_tab_if_added(
    context: Any,
    page_holder: list[Any],
    n_before: int,
    *,
    poll_s: float = 2.6,
    turbo: bool = False,
) -> None:
    """اگر کلیک target=_blank باز کرده باشد، اشاره را به جدیدترین تب منتقل می‌کند."""
    if turbo:
        alive = _playback_alive_pages(context)
        if len(alive) > n_before:
            latest = alive[-1]
            page_holder[0] = latest
        return

    deadline = time.monotonic() + max(0.15, float(poll_s))
    alive: list[Any] = []
    while time.monotonic() < deadline:
        alive = _playback_alive_pages(context)
        if len(alive) > n_before:
            break
        await asyncio.sleep(0.08)
    if len(alive) <= n_before:
        return
    latest = alive[-1]
    try:
        nu = (latest.url or "")[:220]
    except Exception:
        nu = "?"
    get_playback_file_logger().info(
        "$ پس از کلیک تبٔ جدید اضافه شد (%s→%s)؛ ادامهٔ سناریو روی آن تب — url=%s",
        n_before,
        len(alive),
        nu,
    )
    page_holder[0] = latest


def _playback_resolve_active_page(context: Any, cur: Any) -> Any:
    """چند تب / تبٔ خالی (about:blank) / بسته‌شدنٔ تب — رایج در سایت‌هایی که پنجره یا تب جدید باز می‌کنند."""
    alive = _playback_alive_pages(context)
    if not alive:
        return cur
    pb = get_playback_file_logger()
    if len(alive) == 1:
        only = alive[0]
        try:
            if cur is not None and not cur.is_closed() and cur is only:
                return cur
        except Exception:
            pass
        if cur is not only:
            pb.info("$ یک تب زنده مانده؛ اشارهٔ صفحه را با همان تب هماهنگ کردیم")
        return only

    try:
        cur_ok = cur is not None and not cur.is_closed()
    except Exception:
        cur_ok = False
    if not cur_ok:
        try:
            urls = [(_p.url or "")[:100] if not _p.is_closed() else "?" for _p in alive]
            pb.info(
                "$ صفحهٔ جاری بسته بود؛ %s تب زنده — انتقال به آخرین تب | %s",
                len(alive),
                urls,
            )
        except Exception:
            pb.info("$ صفحهٔ جاری بسته بود؛ انتقال به آخرین تب")
        return alive[-1]

    try:
        u = (cur.url or "").strip().lower()
    except Exception:
        u = ""
    if u in ("about:blank", "") or u.startswith(("chrome://", "edge://", "devtools://")):
        for p in reversed(alive):
            if p is cur:
                continue
            try:
                pu = (p.url or "").strip().lower()
                if pu.startswith(("http://", "https://")):
                    pb.info(
                        "$ تب جاری بدون محتوای سایت بود؛ سوئیچ به تب با نشانی %s…",
                        pu[:72],
                    )
                    return p
            except Exception:
                continue
    try:
        if len(alive) > 1:
            dbg = [(p.url or "")[:72] if not p.is_closed() else "?" for p in alive]
            pb.debug("$ چند تب باز است | %s", dbg)
    except Exception:
        pass
    return cur


def _reraise_click_fill_session(exc: BaseException) -> None:
    """خطای «target closed» را به پیام فارسی باز بستهٔ پخش ترجمه می‌کند."""
    if _is_playwright_window_closed_error(exc) or is_target_closed_error(exc):
        raise PlaybackSessionLost(
            "پنجرهٔ مرورگر پخش بسته شده یا از درایور جدا شده. همان یک پنجره‌ای را که برنامه باز کرده تا پایان اجرا باز نگه دار. "
            "اگر وسط کار در صف منتظر ماندی و دستی همان را بستی همین پیام ظاهر می‌شود. "
            "بعد از به‌روزرسانیٔ همین دستیار، اگر سایت با کلیک ورود «تب جدید» می‌گشاید برنامهٔ اجراکنندهٔ سناریو "
            "خودکار به آن تب می‌رود تا روی صفحهٔ قدیمی گیر نکند.",
        ) from exc
    raise exc


async def _playback_activate_page(page: Any) -> None:
    try:
        if page is None or page.is_closed():
            return
        await page.bring_to_front()
    except Exception:
        pass


def _playback_require_live_browser_page(browser: Any, page: Any) -> None:
    """قبل از هر قدم؛ اگر مرورگر مرده باشد همان‌جا خطای قابل‌فهم به‌جای TargetClosed."""
    if browser is not None:
        try:
            if not browser.is_connected():
                raise PlaybackSessionLost(
                    "مرورگر پخش دیگر به درایور وصل نیست (پنجره بسته، کرش، یا قطع ناگهانی). "
                    "همان یک پنجرهٔ کرومیومی که برنامه باز کرده را تا پایان اجرا نبند؛ روی شبکهٔ ناپایدار (DNS/قطع) گاه پروسه کرش می‌کند."
                )
        except PlaybackSessionLost:
            raise
        except Exception:
            pass
    try:
        if page is None:
            raise PlaybackSessionLost("صفحهٔ اتوماسیون نامعتبر است.")
        if page.is_closed():
            raise PlaybackSessionLost(
                "تبٔ اتوماسیون بسته شده است. پنجرهٔ پخش را تا «کار انجام شد» یا خطای واضح باز نگه دار."
            )
    except PlaybackSessionLost:
        raise
    except Exception as exc:
        raise PlaybackSessionLost(f"وضعیت تب مشخص نیست: {exc}") from exc


def _playback_log_step_snapshot(
    pb: logging.Logger,
    *,
    browser: Any,
    context: Any,
    page: Any,
    step_type: str,
    step_detail: str,
) -> None:
    try:
        n_tabs = len(context.pages)
    except Exception:
        n_tabs = -1
    url = "?"
    try:
        if page is not None and not page.is_closed():
            url = (page.url or "")[:200]
    except Exception:
        url = "(url?)"
    conn = "?"
    try:
        if browser is not None:
            conn = str(browser.is_connected())
    except Exception:
        conn = "?"
    pb.info(
        "$ قدم | kind=%s | %s | tabs=%s | browser_ok=%s | url=%s",
        step_type,
        step_detail[:160],
        n_tabs,
        conn,
        url,
    )


async def _playback_solve_challenge_step(
    page: Any,
    step: dict[str, Any],
    *,
    retries: int = 2,
    solver_base_url: str = DEFAULT_SOLVER_BASE_URL,
    solver_model: str = DEFAULT_SOLVER_MODEL,
    log_lines: list[str] | None = None,
    cancel_check: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    """Take screenshot, send to vision AI, execute solve instructions.

    Step fields:
      type: "solve_challenge" | "anticaptcha" | "antibot"
      hint: optional text hint for AI
      retry: max attempts (default 2)
      solver_base_url / solver_model: vision API config
      show_marker: bool (default true)
      delay_ms: pause after solve (default 1500)
    """
    from solver import solve_challenge_screenshot

    pb = get_playback_file_logger()
    hint = str(step.get("hint") or "")
    show_marker = step.get("show_marker", True)

    for attempt in range(1, max(1, int(retries)) + 1):
        if cancel_check and await cancel_check():
            raise PlaybackCancelled("توقف هنگام حل آنتی‌بات.")

        if attempt > 1:
            await asyncio.sleep(1.0)

        try:
            _playback_require_live_browser_page(page.context.browser if hasattr(page, "context") else None, page)
            await page.bring_to_front()

            # screenshot viewport
            bts = await page.screenshot(full_page=False, type="png")
            if log_lines is not None:
                log_lines.append(f"آنتی‌بات: اسکرین‌شات گرفته شد ({len(bts)} bytes), تلاش {attempt}")

            result = await solve_challenge_screenshot(
                bts,
                hint=hint,
                base_url=solver_base_url,
                model=solver_model,
                timeout=90.0,
            )
            ctype = result.get("type", "none")

            if ctype == "none":
                if log_lines is not None:
                    log_lines.append("آنتی‌بات: چالشی دیده نشد (رد شد)")
                return

            pb.info("solve_challenge response: type=%s desc=%s", ctype, result.get("description", "")[:200])
            if log_lines is not None:
                log_lines.append(f"آنتی‌بات: تشخیص={result.get('description', '')[:120]}")

            if ctype == "text_captcha":
                text = result.get("text", "")
                sel = result.get("fill_selector", "")
                if text and sel:
                    loc = page.locator(sel)
                    if show_marker:
                        await _show_action_marker(page, loc, mode="fill", timeout_ms=8000)
                    await loc.fill(text, timeout=10000)
                    if show_marker:
                        await _hide_action_marker(page, 450)
                    if log_lines is not None:
                        log_lines.append(f"آنتی‌بات: متن '{text[:40]}' در {sel} تایپ شد")
                elif text and not sel:
                    # type anywhere focused element
                    await page.keyboard.type(text, delay=50)
                    if log_lines is not None:
                        log_lines.append(f"آنتی‌بات: متن تایپ شد (بدون selector)")
                else:
                    raise RuntimeError(f"solver گفت text_captcha ولی متن یا selector خالی: {result}")

            elif ctype == "image_select":
                click_sels = result.get("click_selectors", [])
                if click_sels:
                    for csel in click_sels:
                        loc = page.locator(csel)
                        if show_marker:
                            await _show_action_marker(page, loc, mode="click", timeout_ms=8000)
                        await loc.click(timeout=10000)
                        if show_marker:
                            await _hide_action_marker(page, 200)
                    if log_lines is not None:
                        log_lines.append(f"آنتی‌بات: {len(click_sels)} تصویر انتخاب شد")
                else:
                    if log_lines is not None:
                        log_lines.append("آنتی‌بات: image_select ولی click_selectors خالی")
                    pb.warning("solver image_select return empty click_selectors: %s", result)

            elif ctype == "checkbox":
                sel = result.get("fill_selector") or (result.get("click_selectors") or [None])[0]
                if sel:
                    loc = page.locator(sel)
                    if show_marker:
                        await _show_action_marker(page, loc, mode="click", timeout_ms=8000)
                    await loc.click(timeout=10000)
                    if show_marker:
                        await _hide_action_marker(page, 400)
                    if log_lines is not None:
                        log_lines.append(f"آنتی‌بات: چک‌باکس {sel} کلیک شد")
                else:
                    if log_lines is not None:
                        log_lines.append("آنتی‌بات: checkbox ولی selector خالی")

            elif ctype == "error":
                err = result.get("description", "خطای solver")
                pb.warning("solve_challenge attempt %s returned error: %s", attempt, err)
                if attempt >= int(retries):
                    raise RuntimeError(f"solver برای آنتی‌بات موفق نشد: {err}")
                continue

            # success — wait settle then return
            delay = float(step.get("delay_ms") or 1500)
            if delay > 0:
                await asyncio.sleep(delay / 1000)
            return

        except Exception as exc:
            pb.warning("solve_challenge attempt %s خطا: %s", attempt, exc)
            if attempt >= int(retries):
                raise RuntimeError(f"حل آنتی‌بات پس {retries} تلاش ناموفق: {exc}") from exc
            continue

    raise RuntimeError("solver: reached end without success (should not happen)")


async def _solve_all_mtcaptcha_iframes(
    page: Any,
    *,
    solver_base_url: str = DEFAULT_SOLVER_BASE_URL,
    solver_model: str = DEFAULT_SOLVER_MODEL,
    log_lines: list[str] | None = None,
) -> int:
    from solver import solve_challenge_screenshot
    import asyncio
    pb = get_playback_file_logger()
    solved = 0

    all_iframe_handles = await page.query_selector_all("iframe")
    mtc_pairs = []
    for h in all_iframe_handles:
        src = (await h.get_attribute("src") or "").lower()
        if "mtcaptcha" in src:
            mtc_pairs.append(h)

    pb.info("کپچا: %d iframe mtcaptcha یافت شد", len(mtc_pairs))

    for i, h in enumerate(mtc_pairs):
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                await h.scroll_into_view_if_needed()
                await asyncio.sleep(0.5)

                bts = await h.screenshot(type="png")

                result = await solve_challenge_screenshot(
                    bts,
                    hint="Read the distorted CAPTCHA text exactly as shown. Return type=text_captcha with the text.",
                    base_url=solver_base_url,
                    model=solver_model,
                    timeout=60.0,
                )
                ctype = result.get("type", "none")
                text = result.get("text", "")
                pb.info("کپچا iframe #%d (تلاش %d): text=%s", i, attempt + 1, text[:30])

                if ctype != "text_captcha" or not text:
                    continue

                try:
                    fr = await h.content_frame()
                except Exception:
                    fr = None
                if fr is None:
                    continue

                inp = fr.locator('input[type="text"], input:not([type])').first
                if await inp.count() == 0:
                    continue
                
                await inp.click(force=True)
                await asyncio.sleep(0.1)
                await inp.evaluate("(el) => el.value = ''")
                await inp.fill(text)
                await inp.evaluate("(el) => { el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }")

                pb.info("کپچا iframe #%d: '%s' تایپ شد", i, text[:30])
                
                # Check for validation
                await asyncio.sleep(2.0)
                html = await fr.content()
                if "error" in html.lower() or "fail" in html.lower() or "invalid" in html.lower():
                    pb.warning("کپچا iframe #%d: ارور تایید! تلاش مجدد...", i)
                    await asyncio.sleep(1.0)
                    continue # Retry loop
                
                # Check if class indicates error
                inp_class = await inp.get_attribute("class") or ""
                if "error" in inp_class.lower() or "invalid" in inp_class.lower():
                    pb.warning("کپچا iframe #%d: اینپوت قرمز شد! تلاش مجدد...", i)
                    await asyncio.sleep(1.0)
                    continue

                # If no error detected, break out of retry loop
                solved += 1
                break

            except Exception as exc:
                pb.warning("کپچا iframe #%d خطا: %s", i, exc)
                break

    return solved


async def _playback_auto_detect_solve(
    page: Any,
    *,
    solver_base_url: str = DEFAULT_SOLVER_BASE_URL,
    solver_model: str = DEFAULT_SOLVER_MODEL,
    solver_retry: int = DEFAULT_SOLVER_RETRY,
    log_lines: list[str] | None = None,
    cancel_check: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    """Take screenshot, check CAPTCHA, solve if found. Return True if solved.
    Uses multi-agent mode — Free model has 3 agents internally.
    """
    from solver import solve_challenge_screenshot

    pb = get_playback_file_logger()

    for attempt in range(1, max(1, int(solver_retry)) + 1):
        if cancel_check and await cancel_check():
            raise PlaybackCancelled("توقف هنگام تشخیص خودکار کپچا.")

        if attempt > 1:
            await asyncio.sleep(1.0)

        try:
            _playback_require_live_browser_page(
                page.context.browser if hasattr(page, "context") else None, page
            )
            await page.bring_to_front()
            bts = await page.screenshot(full_page=False, type="png")
            if log_lines is not None:
                log_lines.append(f"کپچا: بررسی خودکار ({len(bts)} bytes) تلاش {attempt}")

            result = await solve_challenge_screenshot(
                bts,
                base_url=solver_base_url,
                model=solver_model,
                timeout=90.0,
            )
            ctype = result.get("type", "none")
            if ctype == "none":
                if log_lines is not None:
                    log_lines.append("کپچا: تشخیص نشد")
                return False

            pb.info("auto_captcha detect: %s desc=%s", ctype, result.get("description", "")[:200])
            if log_lines is not None:
                log_lines.append(f"کپچا: تشخیص={result.get('description','')[:120]}")

            # solve inline — same logic as _playback_solve_challenge_step
            if ctype == "text_captcha":
                text = result.get("text", "")
                if not text:
                    raise RuntimeError(f"solver گفت text_captcha ولی متن خالی: {result}")
                # اول داخل iframeهای mtcaptcha دنبال textbox بگرد
                filled = False
                for fr in page.frames:
                    furl = (fr.url or "").lower()
                    if "mtcaptcha" not in furl:
                        continue
                    tb = fr.locator('input[type="text"], input:not([type]), textarea')
                    try:
                        cnt = await tb.count()
                    except Exception:
                        cnt = 0
                    if cnt > 0:
                        await tb.first.fill(text, timeout=8000)
                        pb.info("کپچا: متن '%s' در iframe mtcaptcha", text[:40])
                        if log_lines is not None:
                            log_lines.append(f"کپچا: متن '{text[:40]}' در iframe mtcaptcha")
                        filled = True
                        break
                if not filled:
                    # fallback: سلکتور AI یا keyboard
                    sel = result.get("fill_selector", "")
                    if sel:
                        try:
                            await page.locator(sel).fill(text, timeout=5000)
                            filled = True
                            if log_lines is not None:
                                log_lines.append(f"کپچا: متن '{text[:40]}' در {sel}")
                        except Exception:
                            pass
                    if not filled:
                        await page.keyboard.type(text, delay=50)
                        if log_lines is not None:
                            log_lines.append(f"کپچا: متن '{text[:40]}' keyboard")

            elif ctype == "image_select":
                for csel in result.get("click_selectors", []):
                    await page.locator(csel).click(timeout=10000)
                if log_lines is not None:
                    log_lines.append(f"کپچا: {len(result.get('click_selectors',[]))} تصویر کلیک شد")

            elif ctype == "checkbox":
                sel = result.get("fill_selector") or (result.get("click_selectors") or [None])[0]
                if sel:
                    await page.locator(sel).click(timeout=10000)
                    if log_lines is not None:
                        log_lines.append(f"کپچا: چک‌باکس {sel}")

            elif ctype == "error":
                err = result.get("description", "خطا")
                pb.warning("auto_captcha attempt %s error: %s", attempt, err)
                if attempt >= int(solver_retry):
                    raise RuntimeError(f"solver کپچا را تشخیص نداد: {err}")
                continue

            await asyncio.sleep(DEFAULT_SOLVER_PAUSE_AFTER_S)
            if log_lines is not None:
                log_lines.append("کپچا: حل شد")
            return True

        except Exception as exc:
            pb.warning("auto_captcha attempt %s خطا: %s", attempt, exc)
            if attempt >= int(solver_retry):
                raise RuntimeError(f"تشخیص خودکار کپچا پس {solver_retry} تلاش ناموفق: {exc}") from exc
            continue

    return False


async def run_scenario_steps(
    browser: Any,
    context: Any,
    page_holder: list[Any],
    steps: list[dict[str, Any]],
    task_values: dict[str, Any],
    *,
    scenario: dict[str, Any] | None = None,
    defaults: dict[str, Any],
    extra_pause_after_step_ms: float = 0.0,
    show_visual_marker: bool = True,
    goto_retry_max_attempts: int | None = None,
    goto_retry_pause_s: float = PLAYBACK_NAV_RETRY_PAUSE_S,
    goto_per_attempt_timeout_ms: int = PLAYBACK_NAV_GOTO_MS_DEFAULT,
    health_refresh_interval_s: float = 0.0,
    bale_otp_creds: tuple[str, str] | None = None,
    sms_otp_max_wait_s: float = 600.0,
    on_first_page_ready: Callable[[], Awaitable[None]] | None = None,
    cancel_check: Callable[[], Awaitable[bool]] | None = None,
    auto_solve_captcha: bool = DEFAULT_AUTO_SOLVE_CAPTCHA,
    turbo: bool = False,
    turbo_refresh: bool = False,
    refresh_interval_ms: int = 80,
    keep_open_on_missing: bool = True,
    turbo_timeout_s: float = 1800.0,
    start_index: int = 0,
    progress: dict | None = None,
) -> list[str]:
    log_lines: list[str] = []
    vals = dict(defaults or {})
    vals.update(task_values or {})
    sc = dict(scenario or {})

    if turbo or turbo_refresh:
        show_visual_marker = False

    last_health_tick = [time.monotonic()]
    otp_via_bale = False
    for step_i, st in enumerate(steps):
        if progress is not None:
            try:
                progress["failed_index"] = step_i
                progress["failed_id"] = st.get("id") if isinstance(st, dict) else None
            except BaseException:
                pass
        if step_i < (start_index or 0):
            continue
        st_turbo, st_interval, st_keep = _resolve_turbo_params(
            sc,
            st,
            default_turbo=turbo_refresh,
            default_interval=refresh_interval_ms,
            default_keep=keep_open_on_missing,
        )
        page_holder[0] = _playback_resolve_active_page(context, page_holder[0])
        page = page_holder[0]
        await _playback_activate_page(page)

        try:
            _cur_page_url = str(page.url or "")
        except BaseException:
            _cur_page_url = ""

        only_if_url_contains = st.get("only_if_url_contains")
        skip_if_url_contains = st.get("skip_if_url_contains")
        _cond_skip = False

        if only_if_url_contains:
            if isinstance(only_if_url_contains, (list, tuple)):
                if not any(str(c) in _cur_page_url for c in only_if_url_contains if str(c)):
                    _cond_skip = True
            elif str(only_if_url_contains) not in _cur_page_url:
                _cond_skip = True

        if not _cond_skip and skip_if_url_contains:
            if isinstance(skip_if_url_contains, (list, tuple)):
                if any(str(c) in _cur_page_url for c in skip_if_url_contains if str(c)):
                    _cond_skip = True
            elif str(skip_if_url_contains) in _cur_page_url:
                _cond_skip = True

        if _cond_skip:
            log_lines.append(f"قدم {st.get('id')} رد شد (شرط آدرس)")
            try:
                get_playback_file_logger().info("قدم %s رد شد (شرط آدرس: URL=%s)", st.get("id"), _cur_page_url)
            except BaseException:
                pass
            continue

        ty = (st.get("type") or "").lower()
        _dms_raw = st.get("delay_ms")
        if _dms_raw is None:
            dms = 0.0 if (turbo or turbo_refresh) else 400.0
        else:
            dms = float(_dms_raw)
        t_ms = _step_timeout_ms(st)

        raw_for_goto = ""
        if ty == "goto":
            entry_base_check = str(st.get("entry_url_base") or (sc.get("entry_url_base") if step_i == 0 else "") or "").strip()
            raw_for_goto = _playback_resolve(entry_base_check or (st.get("url") or ""), vals)
            detail = f"goto→{raw_for_goto.strip()[:120]}"
        elif ty == "click":
            sel_disp = (st.get('selector') or '')
            if not sel_disp and st.get('selectors'):
                sel_disp = ','.join(str(s) for s in st.get('selectors') if s)
            detail = f"click→{sel_disp[:120]}"
        elif ty == "fill":
            sel_disp = (st.get('selector') or '')
            if not sel_disp and st.get('selectors'):
                sel_disp = ','.join(str(s) for s in st.get('selectors') if s)
            detail = f"fill→{sel_disp[:100]}"
        elif ty == "wait":
            detail = f"wait→{st.get('ms')}ms"
        else:
            detail = ty

        pb_step = get_playback_file_logger()
        _playback_require_live_browser_page(browser, page)

        _playback_log_step_snapshot(
            pb_step,
            browser=browser,
            context=context,
            page=page,
            step_type=ty,
            step_detail=detail,
        )

        if not turbo:
            await _playback_optional_health_reload(
                page,
                interval_s=health_refresh_interval_s,
                last_tick=last_health_tick,
                goto_timeout_ms=goto_per_attempt_timeout_ms,
                log_lines=log_lines,
            )

        if ty == "goto":
            step_entry_base = _playback_resolve(
                str(st.get("entry_url_base") or (sc.get("entry_url_base") if step_i == 0 else "") or ""),
                vals,
            ).strip()
            step_entry_click = str(
                st.get("entry_click")
                or st.get("entry_selector")
                or (sc.get("entry_selector") if step_i == 0 else "")
                or (sc.get("entry_click") if step_i == 0 else "")
                or ""
            ).strip()
            step_entry_selectors = (
                st.get("entry_selectors")
                if isinstance(st.get("entry_selectors"), list)
                else (sc.get("entry_selectors") if (step_i == 0 and isinstance(sc.get("entry_selectors"), list)) else None)
            )

            u = step_entry_base or raw_for_goto.strip() or ""
            if not u:
                raise RuntimeError(
                    "نشانی اولین باز کردن صفحه خالی است؛ فیلدهای «آدرس اجرای پخش/start_url/entry_url_base/goto» یا مقدار placeholderها را چک کن."
                )
            if await _playback_should_skip_redundant_auth_goto(page, u):
                log_lines.append(f"goto رد شد (صفحهٔ OTP از قبل باز است): {u[:60]}")
                continue
            if _playback_should_skip_destructive_goto(page, u, steps, step_i):
                log_lines.append(f"goto رد شد (حفظ صفحهٔ OTP، بدون رفتن به منو): {u[:60]}")
                continue
            nav_cap = max(3000, min(int(goto_per_attempt_timeout_ms), 300_000))
            eff_nav_ms = min(t_ms, nav_cap)
            log_lines.append(
                f"در حال باز کردن نشانی… (هر تلاش حدود {eff_nav_ms // 1000} ثانیه؛ اگر نشد، {int(round(goto_retry_pause_s))} ثانیه صبر و دوباره تا وصل شود یا «توقف اجرا»)",
            )
            tries = await _goto_playback_resilient(
                page,
                u,
                timeout_nav=float(eff_nav_ms),
                max_attempts=goto_retry_max_attempts,
                pause_between_attempts_s=goto_retry_pause_s,
                cancel_check=cancel_check,
                progress_log=log_lines,
                turbo=bool(turbo or turbo_refresh),
            )
            if on_first_page_ready:
                await on_first_page_ready()
            log_lines.append(
                f"صفحه باز شد ({u[:72]}{'…' if len(u) > 72 else ''})"
                + (f" — {tries} تلاش" if tries > 1 else ""),
            )

            # اگر ورود پویا تنظیم شده، روی المان ورودی کلیک کن
            if step_entry_click or step_entry_selectors:
                cands_entry = _resolve_selector_candidates(step_entry_click, step_entry_selectors)
                entry_tag = cands_entry[0] if cands_entry else "?"
                pb_step.info("ورود پویا: کلیک روی المان ورود %r", entry_tag)
                log_lines.append(f"ورود پویا: کلیک روی المان {entry_tag[:40]}…")
                n_before_entry_clk = len(_playback_alive_pages(context))
                await _playback_click_resilient(
                    page,
                    step_entry_click,
                    timeout_ms=t_ms,
                    show_marker=show_visual_marker if not (turbo or st_turbo) else False,
                    max_attempts=goto_retry_max_attempts,
                    pause_between_attempts_s=goto_retry_pause_s,
                    cancel_check=cancel_check,
                    progress_log=log_lines,
                    action_label=f"کلیک ورود پویا {entry_tag[:30]}",
                    turbo=turbo or st_turbo,
                    turbo_refresh=st_turbo,
                    refresh_interval_ms=st_interval,
                    turbo_timeout_s=turbo_timeout_s,
                    selectors=step_entry_selectors,
                )
                await _playback_adopt_new_tab_if_added(context, page_holder, n_before_entry_clk, turbo=turbo or st_turbo)
                page_holder[0] = _playback_resolve_active_page(context, page_holder[0])
                page = page_holder[0]
                log_lines.append(f"ورود پویا: کلیک انجام شد ({entry_tag[:40]})")
            # بعد از باز شدن صفحه: صبر برای رندر JS بعد چک کپچا
            if auto_solve_captcha:
                pb_step.info("کپچا: بررسی اتصال سرویس کپچا...")
                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=2.0) as c:
                        r = await c.get(f"{DEFAULT_SOLVER_BASE_URL.rstrip('/')}/models")
                except Exception:
                    pb_step.info("سرویس کپچا در دسترس نیست؛ رد شدن بدون معطلی.")
                    auto_solve_captcha = False
                pb_step.info("کپچا: شروع تشخیص خودکار…")
                try:
                    # اول همه iframeهای mtcaptcha رو یکی‌یکی حل کن
                    n = await _solve_all_mtcaptcha_iframes(
                        page,
                        solver_base_url=DEFAULT_SOLVER_BASE_URL,
                        solver_model=DEFAULT_SOLVER_MODEL,
                        log_lines=log_lines,
                    )
                    if n == 0:
                        # mtcaptcha iframe نبود — fallback به AI vision عمومی
                        await _playback_auto_detect_solve(
                            page,
                            solver_base_url=DEFAULT_SOLVER_BASE_URL,
                            solver_model=DEFAULT_SOLVER_MODEL,
                            solver_retry=DEFAULT_SOLVER_RETRY,
                            log_lines=log_lines,
                            cancel_check=cancel_check,
                        )
                    pb_step.info("کپچا: تشخیص تمام شد")
                except PlaybackCancelled:
                    raise
                except Exception as exc:
                    pb_step.error("کپچا: خطا — %s", exc)
                    log_lines.append(f"کپچا: خطا — {exc}")

        elif ty == "click":
            # قبل از کلیک: چک کپچا (اگه صفحعه کپچا داره اول حل کن)
            if auto_solve_captcha and not st_turbo:
                try:
                    await _playback_auto_detect_solve(
                        page,
                        solver_base_url=DEFAULT_SOLVER_BASE_URL,
                        solver_model=DEFAULT_SOLVER_MODEL,
                        solver_retry=DEFAULT_SOLVER_RETRY,
                        log_lines=log_lines,
                        cancel_check=cancel_check,
                    )
                except PlaybackCancelled:
                    raise
                except Exception as exc:
                    log_lines.append(f"کپچا: خطا در تشخیص قبل از کلیک — {exc}")
            sel = (st.get("selector") or "").strip()
            selectors = st.get("selectors")
            if not isinstance(selectors, list):
                selectors = None
            cands_ck = _resolve_selector_candidates(sel, selectors)
            if not cands_ck:
                raise RuntimeError("خالی بودن selector برای کلیک")
            primary_sel = cands_ck[0]
            tag = primary_sel[:42] + ("…" if len(primary_sel) > 42 else "")
            if not st_turbo and await _playback_should_skip_redundant_click(page, primary_sel, steps, step_i, vals):
                log_lines.append(
                    f"click رد شد (صفحهٔ ورود آماده است): {primary_sel[:60]}",
                )
                if not (turbo or st_turbo) and PLAYBACK_POST_CLICK_SETTLE_S > 0:
                    await asyncio.sleep(PLAYBACK_POST_CLICK_SETTLE_S)
                continue
            is_optional = bool(st.get("optional"))
            n_before_click = len(_playback_alive_pages(context))
            try:
                tries_ck = await _playback_click_resilient(
                    page,
                    sel,
                    timeout_ms=min(t_ms, 1500) if is_optional else t_ms,
                    show_marker=show_visual_marker if not (turbo or st_turbo) else False,
                    max_attempts=1 if is_optional else goto_retry_max_attempts,
                    pause_between_attempts_s=0.1 if is_optional else goto_retry_pause_s,
                    cancel_check=cancel_check,
                    progress_log=log_lines,
                    action_label=f"کلیک {tag or '?'}",
                    turbo=turbo or st_turbo,
                    turbo_refresh=False if is_optional else st_turbo,
                    refresh_interval_ms=st_interval,
                    turbo_timeout_s=min(turbo_timeout_s, 1.5) if is_optional else turbo_timeout_s,
                    selectors=selectors,
                )
                await _playback_adopt_new_tab_if_added(context, page_holder, n_before_click, turbo=turbo or st_turbo)
                page_holder[0] = _playback_resolve_active_page(context, page_holder[0])
                suf = (f" — {tries_ck} دور تلاش بیرونی" if tries_ck > 1 else "")
                display_sel = sel or (", ".join(cands_ck) if cands_ck else "")
                log_lines.append(f"click {display_sel[:60]}{suf}")
                if not (turbo or st_turbo) and PLAYBACK_POST_CLICK_SETTLE_S > 0:
                    await asyncio.sleep(PLAYBACK_POST_CLICK_SETTLE_S)
                if not (turbo or st_turbo) and _playback_upcoming_sms_otp_fill(steps, step_i) is not None:
                    await asyncio.sleep(PLAYBACK_POST_SMS_SUBMIT_SETTLE_S)
                    log_lines.append("منتظر ارسال پیامک و آماده‌شدن صفحهٔ OTP…")
            except PlaybackCancelled:
                raise
            except BaseException as exc:
                if is_optional:
                    pb_step.info("کلیک اختیاری رد شد (المان وجود نداشت یا پاپ‌آپ نبود): %s", exc)
                    log_lines.append(f"click اختیاری رد شد (پاپ‌آپ یا المان نبود): {primary_sel[:50]}")
                else:
                    raise

        elif ty in ("fill", "type", "type_text"):
            # قبل از fill: چک کپچا
            if auto_solve_captcha and not st_turbo:
                try:
                    await _playback_auto_detect_solve(
                        page,
                        solver_base_url=DEFAULT_SOLVER_BASE_URL,
                        solver_model=DEFAULT_SOLVER_MODEL,
                        solver_retry=DEFAULT_SOLVER_RETRY,
                        log_lines=log_lines,
                        cancel_check=cancel_check,
                    )
                except PlaybackCancelled:
                    raise
                except Exception as exc:
                    log_lines.append(f"کپچا: خطا در تشخیص قبل از fill — {exc}")
            sel = (st.get("selector") or "").strip()
            selectors = st.get("selectors")
            if not isinstance(selectors, list):
                selectors = None
            cands_fl = _resolve_selector_candidates(sel, selectors)
            if not cands_fl:
                raise RuntimeError("selector خالی برای fill")
            primary_sel = cands_fl[0]
            try:
                fill_page_url = (page.url or "").strip()
            except Exception:
                fill_page_url = ""
            if not (turbo or st_turbo) and _playback_url_skips_periodic_health_reload(fill_page_url):
                await asyncio.sleep(PLAYBACK_AUTH_FORM_SETTLE_S)
            if not (turbo or st_turbo):
                await _playback_wait_auth_form_field(page, sel, timeout_ms=t_ms, selectors=selectors)
            p = st.get("param")
            base_v = str(st.get("value") or "")
            if p:
                pv = vals.get(str(p))
                fill_v = str(pv if pv is not None else base_v)
            else:
                fill_v = _playback_resolve(base_v, vals)

            wants_sms_otp = bool(st.get("sms_otp") or st.get("sms_otp_fill"))
            if wants_sms_otp:
                if not bale_otp_creds:
                    raise RuntimeError(
                        "قدم fill با sms_otp علامت خورده ولی توکن بازو یا chat_id بله برای پخش حل نشد؛ "
                        "در بخشٔ «بله / پیام‌رسان» همین دستیار هر دو فیلد را پر کن و ذخیره کن."
                    )
                await _playback_wait_until_otp_screen(
                    page,
                    otp_selector=primary_sel,
                    timeout_ms=max(t_ms, 90_000),
                    cancel_check=cancel_check,
                )
                btok, bchat = bale_otp_creds
                prompt = str(st.get("sms_otp_prompt") or "").strip() or SMS_OTP_BALE_DEFAULT_PROMPT
                try:
                    lo = int(st.get("sms_otp_min_digits") or 4)
                except Exception:
                    lo = 4
                try:
                    hi = int(st.get("sms_otp_max_digits") or 8)
                except Exception:
                    hi = 8
                try:
                    step_wait = float(st.get("sms_otp_wait_seconds") or sms_otp_max_wait_s)
                except Exception:
                    step_wait = sms_otp_max_wait_s
                step_wait = max(30.0, min(float(step_wait), 7200.0))
                log_lines.append(
                    "درخواست کد OTP از بله فرستاده شد؛ لطفاً کد پیامک را در همان چت بازو بفرستید…",
                )
                try:
                    fill_v = await bale_wait_for_sms_code_reply(
                        btok,
                        bchat,
                        prompt,
                        cancel_check=cancel_check,
                        min_digits=lo,
                        max_digits=hi,
                        total_timeout_s=step_wait,
                    )
                except BaleOtpWaitCancelled:
                    raise PlaybackCancelled("توقف هنگام انتظار برای کد پیامک از بله.") from None
                except BaleOtpWaitTimeout as exc:
                    raise RuntimeError(str(exc)) from exc
                otp_via_bale = True

            type_text = (
                ty in ("type", "type_text")
                or bool(st.get("type_text"))
                or bool(st.get("press_sequentially"))
                or int(st.get("char_delay_ms") or 0) > 0
            )
            char_delay_ms = int(st.get("char_delay_ms") or (40 if type_text else 0))
            press_enter = bool(st.get("press_enter") or st.get("enter"))

            tag = primary_sel[:36] + ("…" if len(primary_sel) > 36 else "")
            is_optional_fill = bool(st.get("optional"))
            try:
                tries_fl = await _playback_fill_resilient(
                    page,
                    sel,
                    fill_v,
                    timeout_ms=min(t_ms, 1500) if is_optional_fill else t_ms,
                    show_marker=show_visual_marker if not (turbo or st_turbo) else False,
                    max_attempts=1 if is_optional_fill else goto_retry_max_attempts,
                    pause_between_attempts_s=0.1 if is_optional_fill else goto_retry_pause_s,
                    cancel_check=cancel_check,
                    progress_log=log_lines,
                    action_label=f"فیلد {tag or '?'}",
                    turbo=turbo or st_turbo,
                    turbo_refresh=False if is_optional_fill else st_turbo,
                    refresh_interval_ms=st_interval,
                    turbo_timeout_s=min(turbo_timeout_s, 1.5) if is_optional_fill else turbo_timeout_s,
                    selectors=selectors,
                    type_text=type_text,
                    char_delay_ms=char_delay_ms,
                    press_enter=press_enter,
                )
                suf = (f" — {tries_fl} دور تلاش بیرونی" if tries_fl > 1 else "")
                display_sel = sel or (", ".join(cands_fl) if cands_fl else "")
                log_lines.append(f"fill {display_sel[:48]}{suf}")
            except PlaybackCancelled:
                raise
            except BaseException as exc:
                if is_optional_fill:
                    pb_step.info("فیلد اختیاری رد شد (یافت نشد یا خطا): %s", exc)
                    log_lines.append(f"fill اختیاری رد شد: {primary_sel[:50]}")
                else:
                    raise

        elif ty == "solve_challenge" or ty == "anticaptcha" or ty == "antibot":
            await _playback_solve_challenge_step(
                page,
                st,
                retries=st.get("retry") or st.get("solver_retry") or DEFAULT_SOLVER_RETRY,
                solver_base_url=st.get("solver_base_url") or DEFAULT_SOLVER_BASE_URL,
                solver_model=st.get("solver_model") or DEFAULT_SOLVER_MODEL,
                log_lines=log_lines,
                cancel_check=cancel_check,
            )

        elif ty == "wait":
            ms = float(st.get("ms") or 1000)
            await asyncio.sleep(ms / 1000)
            log_lines.append(f"wait {ms}ms")

        elif ty in ("wait_for_selector", "wait_selector"):
            sel = (st.get("selector") or "").strip()
            selectors = st.get("selectors")
            if not isinstance(selectors, list):
                selectors = None
            cands_w = _resolve_selector_candidates(sel, selectors)
            if not cands_w:
                raise RuntimeError("selector خالی برای wait_for_selector")
            w_timeout = int(st.get("timeout_ms") or 15000)
            log_lines.append(f"منتظر المان {cands_w[0][:40]}…")
            await _playback_first_visible_locator(
                page,
                cands_w[0],
                timeout_ms=w_timeout,
                turbo=turbo or st_turbo,
                selectors=cands_w,
            )
            log_lines.append(f"المان دیده شد ({cands_w[0][:40]})")

        elif ty == "submit_until_confirmed":
            # ارسال سفارش تا تأیید نهایی: کلیک ارسال → مکث → خواندن پیام صفحه →
            # موفق / قطعی / قابل‌تکرار. خطای «خارج از ساعت معاملات» = صبر و تکرار
            # تا ثبت نهایی (کاربر روشن می‌کند و می‌رود؛ توقف با «توقف اجرا»).
            _target_epoch = st.get("target_epoch")
            try:
                _te = float(_target_epoch) if _target_epoch is not None else None
            except (ValueError, TypeError):
                _te = None

            _suc_raw = st.get("success_contains")
            _ret_raw = st.get("retry_contains")
            _fat_raw = st.get("fatal_contains")
            _suc = [str(x) for x in (_suc_raw if isinstance(_suc_raw, list) else []) if str(x)]
            _ret = [str(x) for x in (_ret_raw if isinstance(_ret_raw, list) else []) if str(x)]
            _fat = [str(x) for x in (_fat_raw if isinstance(_fat_raw, list) else []) if str(x)]
            if not _suc:
                _suc = ["ثبت شد", "ثبت سفارش", "موفق", "سفارش شما ثبت", "ارسال شد"]
            if not _ret:
                _ret = ["خارج از ساعت", "بازه زمانی", "محدوده زمانی", "ساعت معاملات", "شروع معاملات", "تلاش مجدد", "معتبر نمی‌باشد"]
            if not _fat:
                _fat = ["موجودی", "اعتبار کافی نیست", "مسدود", "مجاز نیست"]
            try:
                _max_att = max(1, min(int(st.get("max_attempts") or 900), 2000))
            except Exception:
                _max_att = 900
            try:
                _wait_s = min(max(float(st.get("wait_s") or 45), 5.0), 300.0)
            except Exception:
                _wait_s = 45.0
            try:
                _set_s = min(max(float(st.get("settle_s") or 6), 0.0), 30.0)
            except Exception:
                _set_s = 6.0
            _sub_sel = (st.get("selector") or "").strip()
            _sub_sels = st.get("selectors")
            if not isinstance(_sub_sels, list):
                _sub_sels = None
            if not _sub_sel and not _sub_sels:
                raise RuntimeError("selector خالی برای submit_until_confirmed")
            _qty_sel = (st.get("qty_selector") or "").strip()
            _qty_sels = st.get("qty_selectors")
            if not isinstance(_qty_sels, list):
                _qty_sels = None
            _qty_val = str(st.get("qty_value") or "")
            _max_sel = (st.get("max_selector") or "").strip()
            _max_sels = st.get("max_selectors")
            if not isinstance(_max_sels, list):
                _max_sels = None
            # قیمت دلخواه (در اولویت سقف است؛ اگر بود، سقف زده نمی‌شود تا قیمت بازننویسی نشود)
            _prc_sel = (st.get("price_selector") or "").strip()
            _prc_sels = st.get("price_selectors")
            if not isinstance(_prc_sels, list):
                _prc_sels = None
            _prc_val = str(st.get("price_value") or "")
            _att_tmo = min(max(int(t_ms), 3000), 60000)
            _confirmed = False
            for _att in range(1, _max_att + 1):
                if cancel_check and await cancel_check():
                    raise PlaybackCancelled("اجرای سناریو با توقف شما متوقف شد.")
                page_holder[0] = _playback_resolve_active_page(context, page_holder[0])
                _pg = page_holder[0]
                if _qty_sel and _qty_val:
                    _need_fill_qty = True
                    if _att > 1:
                        try:
                            _cur_q = await _pg.evaluate(
                                "(s) => { const el = document.querySelector(s); return el ? (el.value || '') : null; }",
                                _qty_sel,
                            )
                            if str(_cur_q or "").strip() == str(_qty_val).strip():
                                _need_fill_qty = False
                        except Exception:
                            pass
                    if _need_fill_qty:
                        try:
                            await _playback_fill_resilient(
                                _pg, _qty_sel, _qty_val, timeout_ms=min(_att_tmo, 8000),
                                show_marker=False, max_attempts=2, pause_between_attempts_s=0.5,
                                cancel_check=cancel_check, progress_log=None, action_label="تعداد",
                                turbo=True, selectors=_qty_sels,
                            )
                        except PlaybackCancelled:
                            raise
                        except BaseException as exc:
                            raise RuntimeError(f"فرم سفارش در دسترس نیست (تلاش {_att}): {exc}")
                    try:
                        _qv = await _pg.evaluate(
                            "(s) => { const el = document.querySelector(s); return el ? (el.value || '') : null; }",
                            _qty_sel,
                        )
                    except BaseException:
                        _qv = None
                    if _qv is None or str(_qv).strip() == "":
                        raise RuntimeError(
                            f"تعداد در فیلد ننشست (تلاش {_att})؛ مقدار فعلی: «{_qv}»."
                        )
                if _prc_sel and _prc_val:
                    _need_fill_prc = True
                    if _att > 1:
                        try:
                            _cur_p = await _pg.evaluate(
                                "(s) => { const el = document.querySelector(s); return el ? (el.value || '') : null; }",
                                _prc_sel,
                            )
                            if str(_cur_p or "").strip() == str(_prc_val).strip():
                                _need_fill_prc = False
                        except Exception:
                            pass
                    if _need_fill_prc:
                        try:
                            await _playback_fill_resilient(
                                _pg, _prc_sel, _prc_val, timeout_ms=min(_att_tmo, 8000),
                                show_marker=False, max_attempts=2, pause_between_attempts_s=0.5,
                                cancel_check=cancel_check, progress_log=None, action_label="قیمت دلخواه",
                                turbo=True, selectors=_prc_sels,
                            )
                        except PlaybackCancelled:
                            raise
                        except BaseException as exc:
                            raise RuntimeError(f"فیلد قیمت در دسترس نیست (تلاش {_att}): {exc}")
                        # راستی‌آزمایی: مقدار واقعاً نشسته باشد (پرکردن بی‌صدا قبول نیست)؛
                        # اگر نه، یک بار با مسیر غیرتوربو تکرار، وگرنه خطای واضح.
                        try:
                            _pv = await _pg.evaluate(
                                "(s) => { const el = document.querySelector(s); return el ? (el.value || '') : null; }",
                                _prc_sel,
                            )
                        except BaseException:
                            _pv = None
                        if _pv is None or str(_pv).strip() == "":
                            try:
                                _loc2 = await _playback_first_visible_locator(
                                    _pg, _prc_sel, timeout_ms=5000, turbo=False, selectors=_prc_sels,
                                )
                                await _playback_fill_interact(
                                    _loc2, _prc_val, timeout_ms=8000, turbo=False,
                                )
                                _pv = await _pg.evaluate(
                                    "(s) => { const el = document.querySelector(s); return el ? (el.value || '') : null; }",
                                    _prc_sel,
                                )
                            except PlaybackCancelled:
                                raise
                            except BaseException as exc:
                                raise RuntimeError(f"تکرار درج قیمت ناموفق (تلاش {_att}): {exc}")
                            if _pv is None or str(_pv).strip() == "":
                                raise RuntimeError(
                                    f"قیمت در فیلد ننشست (تلاش {_att})؛ مقدار فعلی: «{_pv}»."
                                )
                        log_lines.append(f"قیمت دلخواه درج شد: {_prc_val}")
                elif _max_sel:
                    try:
                        await _playback_click_resilient(
                            _pg, _max_sel, timeout_ms=min(_att_tmo, 8000),
                            show_marker=False, max_attempts=1, pause_between_attempts_s=0.2,
                            cancel_check=cancel_check, progress_log=None, action_label="سقف قیمت",
                            turbo=True, turbo_refresh=False, refresh_interval_ms=st_interval,
                            turbo_timeout_s=turbo_timeout_s, selectors=_max_sels,
                        )
                    except PlaybackCancelled:
                        raise
                    except BaseException:
                        pass

                # انتظار برای زمان هدف (سرخطی / Timer)
                if _te is not None and _att == 1:
                    wait_until_target = _te - time.time()
                    if wait_until_target > 1.5:
                        _wait_msg = (
                            f"⏳ استقرار در فرم سفارش تکمیل شد. در انتظار ثانیه هدف ({time.strftime('%H:%M:%S', time.localtime(_te))}) — {int(wait_until_target)} ثانیه صبر…"
                        )
                        log_lines.append(_wait_msg)
                        get_playback_file_logger().info(_wait_msg)
                        await _sleep_cancellable(wait_until_target - 1.0, cancel_check)
                    while time.time() < _te:
                        if cancel_check and await cancel_check():
                            raise PlaybackCancelled("اجرای سناریو با توقف شما متوقف شد.")
                        rem = _te - time.time()
                        if rem > 0.05:
                            await asyncio.sleep(min(rem - 0.01, 0.05))
                        else:
                            await asyncio.sleep(0.005)

                _clk_msg = f"🚀 کلیک دکمه ارسال خرید در زمان {time.strftime('%H:%M:%S', time.localtime(time.time()))} (هدف: {time.strftime('%H:%M:%S', time.localtime(_te)) if _te else '-'})"
                log_lines.append(_clk_msg)
                get_playback_file_logger().info(_clk_msg)

                try:
                    await _playback_click_resilient(
                        _pg, _sub_sel, timeout_ms=_att_tmo,
                        show_marker=False, max_attempts=2, pause_between_attempts_s=0.5,
                        cancel_check=cancel_check, progress_log=None, action_label="ارسال خرید",
                        turbo=True, turbo_refresh=False, refresh_interval_ms=st_interval,
                        turbo_timeout_s=turbo_timeout_s, selectors=_sub_sels,
                    )
                except PlaybackCancelled:
                    raise
                except BaseException as exc:
                    raise RuntimeError(f"دکمه ارسال یافت نشد (تلاش {_att}): {exc}")

                _is_around_target = bool(_te is not None and time.time() <= _te + 30.0)
                try:
                    await _pg.wait_for_load_state(
                        "networkidle",
                        timeout=int((0.5 if _is_around_target else _set_s) * 1000) if (_is_around_target or _set_s > 0) else 1000,
                    )
                except BaseException:
                    pass
                await _sleep_cancellable(0.2 if _is_around_target else 1.5, cancel_check)
                _body1 = ""
                _url1 = ""
                for _snap_try in range(2):
                    try:
                        _url1 = str(_pg.url or "")
                        _body1 = str(await _pg.evaluate(
                            "(() => { const b = document.body; return (b ? (b.innerText || '').slice(0, 1500) : ''); })()"
                        ))
                    except BaseException:
                        _body1 = ""
                    if any(k in _body1 for k in _suc) or any(k in _body1 for k in _fat) or any(k in _body1 for k in _ret):
                        break
                    if _snap_try == 0:
                        await _sleep_cancellable(0.5 if _is_around_target else 3.0, cancel_check)

                _snap_json = json.dumps({"url": _url1, "body": _body1[:800]}, ensure_ascii=False)
                get_playback_file_logger().info("📸 نتیجه ثبت سفارش: %s", _snap_json)
                log_lines.append(f"📸 نتیجه ثبت سفارش: {_snap_json}")

                if "login" in _url1.lower():
                    raise RuntimeError("نشست منقضی شد (برگشت به صفحه ورود)؛ لطفاً دوباره اجرا کنید.")
                _hit_f = next((k for k in _fat if k in _body1), None)
                if _hit_f:
                    raise RuntimeError(f"خطای قطعی سامانه (تلاش {_att}): «{_hit_f}» — تکرار فایده ندارد.")
                if any(k in _body1 for k in _suc):
                    log_lines.append(f"✅ سفارش ثبت شد (تلاش {_att})")
                    get_playback_file_logger().info(f"✅ سفارش ثبت شد (تلاش {_att})")
                    _confirmed = True
                    break
                if "/order-form/" not in _url1:
                    log_lines.append(f"✅ ارسال انجام و صفحه عوض شد (تلاش {_att}): {_url1[:80]}")
                    get_playback_file_logger().info(f"✅ ارسال انجام و صفحه عوض شد (تلاش {_att}): {_url1[:80]}")
                    _confirmed = True
                    break
                _hit_r = next((k for k in _ret if k in _body1), None)
                _why = f"«{_hit_r}»" if _hit_r else "پیام ناشناخته"
                if _att >= _max_att:
                    raise RuntimeError(f"پس از {_max_att} تلاش ثبت نشد. آخرین وضعیت: {_why}")

                _eff_wait = _wait_s
                if _te is not None and time.time() <= _te + 30.0:
                    if ("خارج از ساعت" in _body1) or ("محدوده زمانی" in _body1) or (_hit_r and any(m in str(_hit_r) for m in ("خارج از ساعت", "ساعت معاملات", "بازه زمانی", "محدوده زمانی"))):
                        _eff_wait = 0.4

                _wait_disp = f"{_eff_wait:.1f}" if _eff_wait < 1.0 else f"{_eff_wait:.0f}"
                _retry_msg = f"⏳ تلاش {_att}/{_max_att} ناموفق ({_why})؛ {_wait_disp} ثانیه صبر…"
                log_lines.append(_retry_msg)
                get_playback_file_logger().info(_retry_msg)
                await _sleep_cancellable(_eff_wait, cancel_check)
            if not _confirmed:
                raise RuntimeError("حلقه ارسال بدون تأیید پایان یافت.")
            log_lines.append("click ارسال خرید (تأییدشده)")

        elif ty in ("snapshot", "dom_snapshot"):
            # عکس‌برداری تشخیصی از وضعیت DOM (مقدار اینپوت، نتایج، اورلی، پیام «یافت نشد»).
            # هم در لاگ فایل هم در خروجی API ثبت می‌شود تا علت ریشه‌ای معلوم شود.
            label_s = str(st.get("label") or st.get("id") or "snapshot")
            js_def = """(() => {
                  const q = (sel) => document.querySelector(sel);
                  const inp = q("input[data-cy=layout-search-input]");
                  const list = q("[data-cy='search-panel-result-list']");
                  const overlays = document.querySelectorAll("[data-cy=bottom-sheet-overlay], .bottom-sheet-overlay, .bottom-sheet");
                  const body_txt = (document.body.innerText || "");
                  return JSON.stringify({
                    input_value: inp ? inp.value : null,
                    input_visible: inp ? (inp.offsetParent !== null) : null,
                    results_text: list ? (list.innerText || "").slice(0, 400) : null,
                    result_links: list ? list.querySelectorAll("a[href*='/stock-details/']").length : null,
                    overlays_visible: Array.from(overlays).filter(o => o.offsetParent !== null).length,
                    not_found: body_txt.includes("نتیجه‌ای یافت نشد"),
                    industries: body_txt.includes("صنایع منتخب"),
                    url: location.href,
                  });
                })()"""
            js = str(st.get("js") or st.get("script") or "").strip() or js_def
            try:
                snap = await page.evaluate(js)
                msg = f"📸 {label_s}: {str(snap)[:800]}"
                log_lines.append(msg)
                try:
                    pb_step.info(msg)
                except BaseException:
                    pass
            except PlaybackCancelled:
                raise
            except BaseException as exc:
                if is_optional:
                    log_lines.append(f"snapshot رد شد (اختیاری): {label_s}")
                else:
                    raise

        elif ty in ("wait_for_hidden", "wait_hidden"):
            # انتظار محو شدن اورلی/شیت مسدودکننده (مثل bottom-sheet مجوز اطلاع‌رسانی).
            # اگر از اول نباشد، فوراً موفق است. اختیاری‌ها در تایم‌اوت فقط رد می‌شوند.
            sel = (st.get("selector") or "").strip()
            selectors = st.get("selectors")
            if not isinstance(selectors, list):
                selectors = None
            cands_h = _resolve_selector_candidates(sel, selectors)
            if not cands_h:
                raise RuntimeError("selector خالی برای wait_for_hidden")
            try:
                h_tmo = float(st.get("timeout_ms") or 10000)
            except Exception:
                h_tmo = 10000.0
            h_tmo = min(max(h_tmo, 1000.0), 60_000.0)
            label_h = str(st.get("label") or st.get("id") or "wait_for_hidden")
            deadline_h = time.monotonic() + h_tmo / 1000.0
            gone = False
            try:
                while time.monotonic() < deadline_h:
                    if cancel_check and await cancel_check():
                        raise PlaybackCancelled("اجرای سناریو با توقف شما متوقف شد.")
                    try:
                        hidden_all = True
                        for _cs in cands_h:
                            try:
                                _lc = page.locator(_cs)
                                if await _lc.count() > 0:
                                    try:
                                        _vis = await _lc.first.is_visible()
                                    except BaseException:
                                        _vis = True
                                    if _vis:
                                        hidden_all = False
                                        break
                            except BaseException:
                                continue
                        if hidden_all:
                            gone = True
                            break
                    except PlaybackCancelled:
                        raise
                    except BaseException:
                        pass
                    await asyncio.sleep(0.05 if (turbo or st_turbo) else 0.25)
            except PlaybackCancelled:
                raise
            except BaseException as exc:
                if is_optional:
                    log_lines.append(f"انتظار محو شدن رد شد (اختیاری): {label_h}")
                    gone = True
                else:
                    raise
            if not gone:
                if is_optional:
                    log_lines.append(f"محو نشدن نادیده گرفته شد (اختیاری): {label_h}")
                else:
                    raise RuntimeError(f"المان مسدودکننده محو نشد ({label_h}): {cands_h[0][:80]}")
            else:
                log_lines.append(f"محو شدن تأیید شد ({label_h})")

        elif ty == "expect_url":
            # تأیید ورود/ناوبری: تا سقف timeout صبر می‌کند آدرس شامل contains شود؛
            # وگرنه با خطای واضح می‌ایستد تا قدم‌های بعدی روی صفحه اشتباه نچرخند.
            want = str(st.get("contains") or st.get("url_contains") or "").strip()
            not_want_raw = str(st.get("not_contains") or "").strip()
            not_contains_items = [x.strip() for x in not_want_raw.split(",") if x.strip()]
            extra_not = st.get("not_contains_list")
            if isinstance(extra_not, list):
                not_contains_items.extend(str(x).strip() for x in extra_not if str(x).strip())
            try:
                tmo = float(st.get("timeout_ms") or 20000)
            except Exception:
                tmo = 20000.0
            tmo = min(max(tmo, 1000.0), 120_000.0)
            label = str(st.get("label") or st.get("id") or "expect_url")
            deadline = time.monotonic() + tmo / 1000.0
            ok_url = False
            cur = ""
            while time.monotonic() < deadline:
                if cancel_check and await cancel_check():
                    raise PlaybackCancelled("اجرای سناریو با توقف شما متوقف شد.")
                try:
                    cur = (page.url or "").strip()
                except Exception:
                    cur = ""
                good = (not want or want in cur) and not any(nw in cur for nw in not_contains_items if nw)
                if good and cur:
                    ok_url = True
                    break
                await asyncio.sleep(0.05 if (turbo or st_turbo) else 0.25)
            if not ok_url:
                raise RuntimeError(
                    f"تأیید صفحه ناموفق بود ({label}): پس از {int(tmo/1000)} ثانیه آدرس «{cur[:120]}» شرط را ندارد."
                    + (" احتمالاً ورود ناموفق بود (یوزر/پسورد یا قفل). قدم‌های بعدی اجرا نشدند." if "ورود" in label or "login" in label.lower() else "")
                )
            log_lines.append(f"تأیید صفحه OK ({label}): {cur[:80]}")

        if _step_notify_bale_login_success(st) and otp_via_bale and bale_otp_creds:
            tok, cid = bale_otp_creds
            try:
                await bale_send_async(tok, cid, "ورود با موفقیت انجام شد.")
                log_lines.append("bale: ورود موفق")
            except Exception as exc:  # noqa: BLE001
                log_lines.append(f"bale: ارسال ورود موفق ناموفق — {str(exc)[:120]}")
                get_playback_file_logger().warning(
                    "ارسال پیام «ورود موفق» به بله ناموفق: %s",
                    exc,
                    exc_info=True,
                )
            otp_via_bale = False

        # اگر کاربر در فرم یا تسک مکث مشخصی (extra_pause_after_step_ms) تعیین کرده باشد، دقیقا همان اعمال می‌شود
        # در غیر این صورت delay_ms مرحله اعمال می‌شود. اگر هر دو صفر باشند، بدون کوچکترین معطلی رد می‌شود.
        if turbo or st_turbo:
            effective_pause = 0.0 if "delay_ms" not in st and extra_pause_after_step_ms <= 0 else (extra_pause_after_step_ms if extra_pause_after_step_ms > 0 else float(st.get("delay_ms") or 0))
        elif extra_pause_after_step_ms > 0:
            effective_pause = extra_pause_after_step_ms
        else:
            _em_raw = st.get("delay_ms")
            effective_pause = float(_em_raw) if _em_raw is not None else float(dms or 0)
        if effective_pause > 0:
            await asyncio.sleep(min(effective_pause / 1000, 300))

    return log_lines


def _playwright_obj_is_closed(obj: Any) -> bool:
    try:
        fn = getattr(obj, "is_closed", None)
        if callable(fn):
            return bool(fn())
    except Exception:
        return False
    return False


def _playwright_browser_impl_channel(browser: Any) -> Any | None:
    impl = getattr(browser, "_impl_obj", None)
    if impl is None:
        return None
    return getattr(impl, "_channel", None)


def _windows_kill_playback_chromium_profile(profile_dir: Path, pb: logging.Logger) -> None:
    """وقتی کانال Playwright قطع است، kill روی خود ویندوز با تطبیق CommandLine (--user-data-dir=…)."""
    if sys.platform != "win32":
        return
    try:
        marker = str(profile_dir.resolve())
    except Exception:
        marker = str(profile_dir)
    if len(marker) < 12:
        return
    esc = marker.replace("'", "''").replace("`", "``")
    ps = (
        f"$m = [regex]::Escape('{esc}'); "
        "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.CommandLine -and $_.CommandLine -match $m } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if r.returncode != 0 and (r.stderr or "").strip():
            pb.debug("$ ویندوز kill پروفایل | stderr=%s", (r.stderr or "")[:500])
        pb.info("$ ویندوز: پروسه‌های کروم این نشست (پوشهٔ پروفایل) متوقف شدند")
    except Exception as exc:
        pb.warning("$ ویندوز: دستور متوقف‌سازی پروفایل پخش اجرا نشد | %s", exc)


async def _playwright_browser_force_kill(browser: Any, pb: logging.Logger) -> bool:
    """killForTests در درایور Node — درخت پروسهٔ مرورگر مقطوع؛ وقتی browser.close معطل می‌شود."""
    ch = _playwright_browser_impl_channel(browser)
    if ch is None:
        pb.info("$ killForTests: بدون کانال (احتمالاً نشست از قبل تمام شده)")
        return True
    try:
        pb.info("$ Playwright.killForTests — تضمین بستن پروسهٔ Chromium این نشست")
        await ch.send("killForTests", None, {})
        return True
    except Exception as exc:
        if is_target_closed_error(exc) or "has been closed" in str(exc).lower():
            pb.debug("killForTests: نشست از قبل بسته بود | %s", exc)
            return True
        pb.warning("killForTests ناموفق: %s", exc)
        return False


async def _ensure_playwright_browser_dead(
    browser: Any,
    pb: logging.Logger,
    *,
    graceful_close_returned_ok: bool,
) -> bool:
    """همیشه یک بار killForTests؛ روی ویندوز گاهاً browser.close برمی‌گردد ولی پنجره می‌ماند."""
    did_kill = False
    if graceful_close_returned_ok:
        await asyncio.sleep(0.2)
    did_kill = await _playwright_browser_force_kill(browser, pb)
    await asyncio.sleep(0.15)
    try:
        ic = getattr(browser, "is_connected", None)
        if callable(ic):
            return not ic()
    except Exception:
        pass
    return bool(did_kill)


async def _shutdown_playback_chromium(
    page: Any,
    context: Any,
    browser: Any,
    *,
    pb: logging.Logger,
    scenario_finished_ok: bool,
    playback_profile_dir: Path | None = None,
) -> bool:
    """بستن کرومیوم پخش تا حد ممکن؛ در صورت گیر، kill اجباری درایور Playwright.

    پس از موفقیت اول browser.close مستقیم امتحان می‌شود تا گیرِ page/context کمتر شود.

    با ``playback_profile_dir`` روی ویندوز یک Stop-Process هم روی خط فرمان واقعی Chromium زده می‌شود
    (برای زمانی که killForTests به‌خاطر قطع کانال جواب ندهد ولی پنجره مانده باشد).
    """
    rb = (
        "SiteWorkflowAssistant: سناریو بدون خطا تمام شد؛ بستن مرورگر پخش."
        if scenario_finished_ok
        else "SiteWorkflowAssistant: پایان جلسهٔ پخش (توقف یا خطا)."
    )

    async def _browser_close_once() -> None:
        await browser.close(reason=rb)

    async def _graceful_browser_close_attempt() -> bool:
        """True اگر بازگشت await بدون TimeoutError؛ ممکن است با خطای «قبلاً بسته» هم True شود."""
        try:
            await asyncio.wait_for(
                _browser_close_once(),
                timeout=_PLAYBACK_CLOSE_BROWSER_TIMEOUT_S,
            )
            return True
        except asyncio.TimeoutError:
            pb.error(
                "browser.close بیش از %ss بدون بازگشت (احتمال گیر؛ kill اجباری امتحان می‌شود)",
                _PLAYBACK_CLOSE_BROWSER_TIMEOUT_S,
            )
            return False
        except Exception as exc:
            pb.warning(
                "browser.close: %s (اگر نشست قطع شده، طبیعی است)",
                exc,
            )
            return True

    out = False
    try:
        skip_chain = False
        if scenario_finished_ok:
            pb.info("$ بستن پس از موفقیت — اول browser.close مستقیم")
            graceful_ok = await _graceful_browser_close_attempt()
            if graceful_ok:
                out = await _ensure_playwright_browser_dead(browser, pb, graceful_close_returned_ok=True)
                skip_chain = True
            else:
                pb.warning("بستن مستقیم کامل نشد؛ ادامه با زنجیر page→context→browser")

        if not skip_chain:
            pb.info("$ بستن زنجیر مرورگر پخش (page بدون قبل‌ازunload → context → browser)")

            if not _playwright_obj_is_closed(page):
                try:
                    await asyncio.wait_for(
                        page.close(run_before_unload=False, reason=rb),
                        timeout=_PLAYBACK_CLOSE_PAGE_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    pb.warning(
                        "page.close بیش از %ss؛ ادامه با context/browser",
                        _PLAYBACK_CLOSE_PAGE_TIMEOUT_S,
                    )
                except Exception as exc:
                    pb.warning("page.close: %s", exc)

            if not _playwright_obj_is_closed(context):
                try:
                    await asyncio.wait_for(
                        context.close(reason=rb),
                        timeout=_PLAYBACK_CLOSE_CONTEXT_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    pb.warning(
                        "context.close بیش از %ss؛ ادامهٔ بستن با browser.close",
                        _PLAYBACK_CLOSE_CONTEXT_TIMEOUT_S,
                    )
                except Exception as exc:
                    pb.warning("context.close: %s", exc)

            graceful_ok = await _graceful_browser_close_attempt()
            out = await _ensure_playwright_browser_dead(
                browser, pb, graceful_close_returned_ok=graceful_ok
            )
    finally:
        if playback_profile_dir is not None:
            try:
                await asyncio.to_thread(_windows_kill_playback_chromium_profile, playback_profile_dir, pb)
            except Exception as exc:
                pb.warning("$ ویندوز: پاکسازی پروفایل پخش خطا خورد | %s", exc)
    return out


def _coerce_keep_browser_on_failure(raw: Any) -> bool:
    """پیش‌فرض: بعد از خطا مرورگر بسته شود. true با bool/رشته/عدد صریح برای نگه‌داشتن پنجرهٔ خطا."""
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("0", "false", "no", "off", ""):
            return False
        if s in ("1", "true", "yes", "on"):
            return True
    return bool(raw)


def _resolve_extra_step_pause_ms(
    scenario: dict[str, Any],
    api_ms: float | None,
) -> float:
    if api_ms is not None:
        return max(0.0, float(api_ms))
    if "pause_between_steps_ms" in scenario:
        return max(0.0, float(scenario.get("pause_between_steps_ms") or 0))
    return DEFAULT_PAUSE_BETWEEN_STEPS_MS


def _resolve_sms_otp_wait_seconds_from_scenario(scenario: dict[str, Any]) -> float:
    """سقف انتظار برای کد پیامک از بله (ثانیه؛ بین ۳۰ و ۷۲۰۰)."""
    raw = scenario.get("sms_otp_wait_seconds")
    if raw is None or raw == "":
        return 600.0
    try:
        v = float(raw)
    except Exception:
        return 600.0
    return max(30.0, min(v, 7200.0))


def _resolve_scenario_entry_url(
    scenario: dict[str, Any],
    playback_start_url: str | None,
    defaults: dict[str, Any] | None = None,
) -> str | None:
    """نشانی شروع یک سناریو برای انتقال بین قلم‌های صف (بدون بستن مرورگر)."""
    defs = dict(defaults or {})
    preferred = _resolve_playback_http_url(playback_start_url or "", defs)
    if preferred is None:
        preferred = _resolve_playback_http_url(str(scenario.get("entry_url_base") or ""), defs)
    if preferred is None:
        preferred = _resolve_playback_http_url(str(scenario.get("start_url") or ""), defs)
    if preferred is not None:
        return preferred
    for st in scenario.get("steps") or []:
        if (st.get("type") or "").lower() != "goto":
            continue
        u_base = _playback_resolve(str(st.get("entry_url_base") or ""), defs).strip()
        if u_base.lower().startswith(("http://", "https://")):
            return u_base
        u = _playback_resolve(str(st.get("url") or ""), defs).strip()
        if u.lower().startswith(("http://", "https://")):
            return u
    return None


async def _playback_handoff_to_entry(
    page: Any,
    entry_url: str,
    *,
    goto_timeout_ms: int,
    goto_retry_max_attempts: int | None,
    goto_retry_pause_s: float,
    cancel_check: Callable[[], Awaitable[bool]] | None,
    progress_log: list[str] | None,
) -> None:
    target = _sanitize_url(entry_url)
    try:
        cur = (page.url or "").strip()
    except Exception:
        cur = ""
    if cur and _urls_equivalent(cur, target):
        get_playback_file_logger().info("$ handoff: از قبل روی نشانی شروع | %s", cur[:160])
        return
    get_playback_file_logger().info("$ handoff به سناریوی بعدی | %s", target[:160])
    if progress_log is not None:
        progress_log.append(f"انتقال به شروع سناریوی بعدی: {target[:72]}{'…' if len(target) > 72 else ''}")
    eff = float(min(max(3000, int(goto_timeout_ms)), 120_000))
    await _goto_playback_resilient(
        page,
        target,
        timeout_nav=eff,
        max_attempts=goto_retry_max_attempts,
        pause_between_attempts_s=goto_retry_pause_s,
        cancel_check=cancel_check,
        progress_log=progress_log,
    )
    if _playback_url_skips_periodic_health_reload(target):
        await asyncio.sleep(PLAYBACK_AUTH_FORM_SETTLE_S)


def _resolve_playback_health_refresh_s(
    scenario: dict[str, Any],
    api_seconds: float | None,
) -> float:
    if api_seconds is not None:
        return max(0.0, float(api_seconds))
    raw = scenario.get("playback_health_refresh_interval_s")
    if raw is None or raw == "":
        return DEFAULT_PLAYBACK_HEALTH_REFRESH_INTERVAL_S
    try:
        return max(0.0, float(raw))
    except Exception:
        return DEFAULT_PLAYBACK_HEALTH_REFRESH_INTERVAL_S


async def run_playback(
    scenario: dict[str, Any],
    runs_input: list[dict[str, Any]] | None = None,
    *,
    playback_start_url: str | None = None,
    pause_between_steps_ms: float | None = None,
    playback_health_refresh_interval_s: float | None = None,
    bale_send: Callable[[str], Awaitable[None]] | None = None,
    cancel_check: Callable[[], Awaitable[bool]] | None = None,
    on_playback_registry_teardown: Callable[[], None] | None = None,
    bale_otp_creds: tuple[str, str] | None = None,
    sms_otp_max_wait_s: float | None = None,
    turbo_mode: bool | None = None,
    reusable_context: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    runs = runs_input if runs_input is not None else list(scenario.get("runs") or [])
    if not runs:
        runs = [{"label": "تک‌بار", "values": {}}]

    turbo_refresh, refresh_interval_ms, keep_open_on_missing = _resolve_turbo_params(scenario)
    turbo_timeout_s = float(scenario.get("turbo_timeout_s") or scenario.get("turbo_timeout_seconds") or 1800.0)
    turbo = is_turbo_mode(scenario, turbo_mode) or turbo_refresh
    extra_step_pause = 0.0 if turbo else _resolve_extra_step_pause_ms(scenario, pause_between_steps_ms)
    health_refresh_s = (
        0.0
        if turbo
        else _resolve_playback_health_refresh_s(
            scenario,
            playback_health_refresh_interval_s,
        )
    )
    sms_otp_wait_s = (
        float(sms_otp_max_wait_s)
        if sms_otp_max_wait_s is not None
        else _resolve_sms_otp_wait_seconds_from_scenario(scenario)
    )
    sms_otp_wait_s = max(30.0, min(float(sms_otp_wait_s), 7200.0))
    raw_nav = scenario.get("nav_retry_max_attempts")
    nav_max: int | None = None
    if raw_nav is not None and raw_nav != "":
        try:
            v = int(float(raw_nav))
            if v <= 1:
                nav_max = None
                if v == 1:
                    log.info(
                        "nav_retry_max_attempts=1 نادیده گرفته شد (برای واقعاً نامحدود نبودن سناریو، حذف کلید یا مقدار ≥۲ بگذار)"
                    )
            else:
                nav_max = min(v, 2_147_483_647)
        except Exception:
            nav_max = None

    try:
        nav_pause = float(scenario.get("nav_retry_pause_seconds") or PLAYBACK_NAV_RETRY_PAUSE_S)
    except Exception:
        nav_pause = PLAYBACK_NAV_RETRY_PAUSE_S
    nav_pause = max(1.0, min(nav_pause, 120.0))

    try:
        nav_goto_ms = int(float(scenario.get("nav_retry_goto_timeout_ms") or PLAYBACK_NAV_GOTO_MS_DEFAULT))
    except Exception:
        nav_goto_ms = PLAYBACK_NAV_GOTO_MS_DEFAULT
    nav_goto_ms = max(3000, min(nav_goto_ms, 300_000))

    steps_all = playback_steps_with_entry(scenario, playback_start_url=playback_start_url)
    show_marker = False if turbo else _scenario_show_visual_marker(scenario)
    captcha_enabled = bool(scenario.get("auto_solve_captcha")) if turbo else scenario.get("auto_solve_captcha", DEFAULT_AUTO_SOLVE_CAPTCHA)
    globals_default = scenario.get("default_values") or {}

    peek_goto = [
        str((s.get("url") or "")).strip()[:140]
        for s in steps_all
        if (s.get("type") or "").lower() == "goto"
    ][:4]
    pb = get_playback_file_logger()
    pb.info(
        "=== شروع run_playback | turbo=%s | log_file=%s | API_start_url=%r | step_count=%s | nav_max=%s nav_pause_s=%s goto_timeout_ms=%s | step_pause_extra_ms=%s health_refresh_s=%s | keep_browser(raw)=%r",
        turbo,
        str(get_playback_log_file_path()),
        playback_start_url,
        len(steps_all),
        nav_max,
        nav_pause,
        nav_goto_ms,
        extra_step_pause,
        health_refresh_s,
        scenario.get("keep_browser_on_failure"),
    )
    pb.debug("gotoهای اول سناریو (پیش‌نمایش): %s", peek_goto)
    if not bale_otp_creds:
        for s in steps_all:
            if s.get("sms_otp") or s.get("sms_otp_fill"):
                pb.warning(
                    "سناریو قدم با sms_otp دارد اما توکن بازو یا Chat ID به پخش نرسیده — "
                    "در بخش ۲ همین صفحه هر دو را پر کن و «ذخیرهٔ همه چیز روی دیسک» بزن؛ "
                    "تا وقتی خالی باشد بله برای گرفتن کد پیامک صدا زده نمی‌شود.",
                )
                break

    async def _playback_body() -> dict[str, Any]:
        try:
            global _playback_shared_context, _playback_shared_profile_dir
            playback_profile_dir: Path | None = None
            can_reuse = bool(scenario.get("reuse_browser") or scenario.get("keep_browser_open"))
            context = None
            if reusable_context is not None and _is_context_alive(reusable_context):
                context = reusable_context
            elif can_reuse and _playback_shared_context is not None and _is_context_alive(_playback_shared_context):
                context = _playback_shared_context
                playback_profile_dir = _playback_shared_profile_dir

            if context is None:
                try:
                    pw = await launch_playwright()
                except BaseException:
                    get_playback_file_logger().exception("خطا در launch_playwright (قبل از مرورگر)")
                    raise
                playback_profile_dir = Path(tempfile.mkdtemp(prefix="swa_playback_"))
                sw_policy = "block" if scenario.get("block_service_workers") else None
                ctx_kwargs: dict[str, Any] = {
                    "headless": False,
                    "locale": "fa-IR",
                    "viewport": scenario.get("viewport") or {"width": 1280, "height": 800},
                    "args": CHROMIUM_TURBO_ARGS,
                }
                if sw_policy:
                    ctx_kwargs["service_workers"] = sw_policy
                try:
                    context = await pw.chromium.launch_persistent_context(
                        str(playback_profile_dir.resolve()),
                        **ctx_kwargs,
                    )
                except BaseException:
                    shutil.rmtree(playback_profile_dir, ignore_errors=True)
                    raise
            browser = context.browser
            _pp = [p for p in context.pages if not p.is_closed()]
            page_holder: list[Any] = [_pp[0]] if _pp else [await context.new_page()]

            # شنود خطاهای کنسول/صفحه برای علت‌یابی خرابی‌ها (آخرین ۳۰ مورد نگه داشته می‌شود).
            console_errors: list[str] = []
            try:
                if not getattr(context, "_forensics_hooked", False):
                    def _forensics_on_console(msg: Any) -> None:
                        try:
                            if getattr(msg, "type", "") in ("error", "warning"):
                                console_errors.append(f"[{msg.type}] {msg.text}"[:300])
                                del console_errors[:-30]
                        except BaseException:
                            pass

                    def _forensics_on_pageerror(exc: Any) -> None:
                        try:
                            console_errors.append(f"[pageerror] {exc}"[:300])
                            del console_errors[:-30]
                        except BaseException:
                            pass

                    def _forensics_hook_page(pg: Any) -> None:
                        try:
                            pg.on("console", _forensics_on_console)
                            pg.on("pageerror", _forensics_on_pageerror)
                        except BaseException:
                            pass

                    context.on("page", _forensics_hook_page)
                    for _pg in context.pages:
                        _forensics_hook_page(_pg)
                    try:
                        setattr(context, "_forensics_hooked", True)
                    except BaseException:
                        pass
            except BaseException:
                pass

            close_browser_when_done = True
            browser_left_open = False
            results: list[dict[str, Any]] = []

            site_ready_sent = False
            finished_sent = False

            async def notify_site_ready_once() -> None:
                nonlocal site_ready_sent
                if not bale_send or site_ready_sent:
                    return
                site_ready_sent = True
                try:
                    await bale_send("سایت وصل شد؛ در حال انجام سناریو هستیم.")
                except Exception:
                    log.warning("ارسال پیام بله «سایت وصل شد» ناموفق", exc_info=True)

            async def notify_finished() -> None:
                nonlocal finished_sent
                if not bale_send or finished_sent:
                    return
                finished_sent = True
                try:
                    await bale_send("سناریو انجام شد و تمام شد.")
                except Exception:
                    log.warning("ارسال پیام بله «سناریو تمام شد» ناموفق", exc_info=True)

            scenario_reached_end = False
            browser_close_failed = False
            try:
                for ri, run_one in enumerate(runs):
                    label = run_one.get("label") or f"اجرای {ri + 1}"
                    task_vals = dict(run_one.get("values") or {})
                    # حلقه ریکاوری هوشمند: اگر قدمی (غیرلغو) خطا داد، صفحه رفرش می‌شود،
                    # موقعیت فعلی از روی آدرس تشخیص داده می‌شود و اجرا از قدم متناظر
                    # (resume_markers سناریو) ادامه می‌یابد؛ نه از اول، نه با شکست.
                    _resume_markers = scenario.get("resume_markers") or []
                    try:
                        _recovery_max = max(0, int(scenario.get("recovery_max") or 0))
                    except Exception:
                        _recovery_max = 0
                    _recoveries_done = 0
                    _resume_from = 0
                    _accum_logs: list[str] = []
                    _last_resume_target = -1
                    _stuck_rounds = 0
                    while True:
                        _progress: dict = {}
                        try:
                            log_lines = await run_scenario_steps(
                                browser,
                                context,
                                page_holder,
                                steps_all,
                                task_vals,
                                scenario=scenario,
                                defaults=globals_default,
                                extra_pause_after_step_ms=extra_step_pause,
                                show_visual_marker=show_marker,
                                goto_retry_max_attempts=nav_max,
                                goto_retry_pause_s=nav_pause,
                                goto_per_attempt_timeout_ms=nav_goto_ms,
                                health_refresh_interval_s=health_refresh_s,
                                bale_otp_creds=bale_otp_creds,
                                sms_otp_max_wait_s=sms_otp_wait_s,
                                on_first_page_ready=notify_site_ready_once,
                                cancel_check=cancel_check,
                                auto_solve_captcha=captcha_enabled,
                                turbo=turbo,
                                turbo_refresh=turbo_refresh,
                                refresh_interval_ms=refresh_interval_ms,
                                keep_open_on_missing=keep_open_on_missing,
                                turbo_timeout_s=turbo_timeout_s,
                                start_index=_resume_from,
                                progress=_progress,
                            )
                            _accum_logs.extend(log_lines)
                            log_lines = _accum_logs
                            break
                        except PlaybackCancelled:
                            raise
                        except Exception as exc:
                            _fidx = _progress.get("failed_index", -1)
                            _fid = _progress.get("failed_id", "")
                            try:
                                _fidx = int(_fidx)
                            except Exception:
                                _fidx = -1
                            _failed_step = next(
                                (s for s in steps_all if isinstance(s, dict) and s.get("id") == _fid),
                                None,
                            )
                            # قدم نهایی خرید (no_resume) یا اتمام بودجه ریکاوری = شکست قطعی.
                            if _failed_step is not None and _failed_step.get("no_resume"):
                                raise
                            if _recoveries_done >= _recovery_max or not _resume_markers or _fidx < 0:
                                raise
                            try:
                                _rpg = page_holder[0] if page_holder else None
                                if _rpg is None:
                                    raise RuntimeError("no live page for recovery")
                                await _rpg.reload(wait_until="domcontentloaded", timeout=20000)
                                await asyncio.sleep(1.0)
                                page_holder[0] = _playback_resolve_active_page(context, _rpg)
                                _rpg = page_holder[0]
                                _cur_url = str(_rpg.url or "")
                            except Exception:
                                raise
                            _id2idx = {
                                s.get("id"): i for i, s in enumerate(steps_all)
                                if isinstance(s, dict) and s.get("id")
                            }
                            _target = -1
                            for _mk in _resume_markers:
                                try:
                                    _uc = str((_mk or {}).get("url_contains") or "")
                                    _ti = _id2idx.get((_mk or {}).get("resume_at"), -1)
                                    if _uc and _uc in _cur_url and 0 <= _ti <= _fidx and _ti > _target:
                                        _target = _ti
                                except BaseException:
                                    continue
                            if _target < 0:
                                raise
                            if _target == _last_resume_target:
                                _stuck_rounds += 1
                            else:
                                _stuck_rounds = 0
                                _last_resume_target = _target
                            if _stuck_rounds >= 1:
                                raise
                            _recoveries_done += 1
                            _resume_from = _target
                            _rlog = (
                                f"♻️ ریکاوری #{_recoveries_done}: رفرش صفحه و ادامه از قدم "
                                f"«{(_id2idx and [k for k, v in _id2idx.items() if v == _target][0])}» "
                                f"(آدرس: {_cur_url[:90]})"
                            )
                            _accum_logs.append(_rlog)
                            try:
                                get_playback_file_logger().info(_rlog)
                            except BaseException:
                                pass
                            continue
                    results.append({"label": label, "ok": True, "log": log_lines})
                    _pb_raw = scenario.get("pause_between_tasks_ms")
                    pause_between = 0.0 if turbo else (float(_pb_raw) if _pb_raw is not None else 1200.0)
                    if pause_between > 0 and ri < len(runs) - 1:
                        await _sleep_cancellable(pause_between / 1000, cancel_check)
                scenario_reached_end = True
                # فرصت نشستن درخواست نهایی (مثل POST ثبت سفارش) + عکس مدرک موفقیت.
                # بدون این مکث، بستن فوری مرورگر درخواست درحال‌پرواز را می‌کشد و
                # «کلیک شد» هرگز به «ثبت شد» تبدیل نمی‌شود.
                try:
                    _settle_s = float(scenario.get("settle_before_close_s") or 0)
                except Exception:
                    _settle_s = 0.0
                _settle_s = min(max(_settle_s, 0.0), 30.0)
                if _settle_s > 0:
                    try:
                        _spg = page_holder[0] if page_holder else None
                        if _spg is not None:
                            try:
                                await _spg.wait_for_load_state("networkidle", timeout=int(_settle_s * 1000))
                            except BaseException:
                                pass
                            try:
                                _sdir = LOGS_DIR / "forensics"
                                _sdir.mkdir(parents=True, exist_ok=True)
                                _sname = f"success_{int(time.time())}.png"
                                await _spg.screenshot(path=str(_sdir / _sname), full_page=False)
                                get_playback_file_logger().info("📸 اسکرین‌شات موفقیت: %s", _sname)
                            except BaseException:
                                pass
                    except BaseException:
                        pass
                close_browser_when_done = True
            except PlaybackSessionLost as exc:
                get_playback_file_logger().warning("قطع نشستٔ مرورگر قبل از اتمام سناریو: %s", exc)
                results.append(
                    {
                        "label": "مرورگر یا تب بسته شد",
                        "ok": False,
                        "error": str(exc),
                        "playback_window_closed": True,
                    }
                )
                keep_open = _coerce_keep_browser_on_failure(scenario.get("keep_browser_on_failure")) or keep_open_on_missing
                close_browser_when_done = not keep_open
            except PlaybackCancelled as exc:
                get_playback_file_logger().warning("توقف توسط کاربر (PlaybackCancelled) | %s", exc)
                results.append(
                    {
                        "label": "توقف توسط کاربر",
                        "ok": False,
                        "error": str(exc),
                        "cancelled": True,
                    }
                )
                close_browser_when_done = True
            except asyncio.CancelledError:
                get_playback_file_logger().error(
                    "asyncio.CancelledError داخل تسک پخش — ممکن است سرور در حال خاموش‌شدن باشد",
                )
                log.warning(
                    "تسک پخش در سطح asyncio لغو شد (معمولاً خاموش‌شدن سرور یا قطعِ نادر اتصال).",
                )
                results.append(
                    {
                        "label": "اجرای قطع‌شده توسط سیستم",
                        "ok": False,
                        "error": (
                            "اجرای سناریو از طرف میزبان asyncio لغو شد؛ برگهٔ دستیار را باز نگه دار تا درخواست قطع نشود."
                        ),
                        "cancelled": True,
                    }
                )
                close_browser_when_done = True
            except Exception as exc:
                get_playback_file_logger().exception(
                    "خطا در اجرای سناریو (به‌عنوان «پایان با خطا» به UI برمی‌گردد) | repr=%r",
                    exc,
                )
                row: dict[str, Any] = {"label": "پایان با خطا", "ok": False, "error": str(exc)}
                # کالبدشکافی لحظه خطا: آدرس، عنوان، متن صفحه، خطاهای کنسول، اسکرین‌شات.
                # تا علت گیر کردن با مدرک در لاگ و پاسخ API باشد، نه حدس.
                try:
                    _fpg = None
                    try:
                        _fpg = page_holder[0] if page_holder else None
                    except BaseException:
                        _fpg = None
                    if _fpg is not None:
                        try:
                            row["forensics_url"] = str(_fpg.url or "")[:200]
                        except BaseException:
                            pass
                        try:
                            row["forensics_title"] = str(await _fpg.title())[:200]
                        except BaseException:
                            pass
                        try:
                            _snap = await _fpg.evaluate(
                                "(() => { const b = document.body; return (b ? (b.innerText || '').slice(0, 400) : 'NO_BODY'); })()"
                            )
                            row["forensics_body"] = str(_snap)[:500]
                        except BaseException:
                            pass
                        try:
                            if console_errors:
                                row["forensics_console"] = list(console_errors[-12:])
                        except BaseException:
                            pass
                        try:
                            _fdir = LOGS_DIR / "forensics"
                            _fdir.mkdir(parents=True, exist_ok=True)
                            _fname = f"fail_{int(time.time())}.png"
                            await _fpg.screenshot(path=str(_fdir / _fname), full_page=False)
                            row["forensics_screenshot"] = _fname
                            get_playback_file_logger().info("📸 اسکرین‌شات خطا: %s", _fname)
                        except BaseException:
                            pass
                        get_playback_file_logger().info(
                            "🔍 کالبدشکافی خطا | url=%s | title=%s | body=%s | console=%s",
                            row.get("forensics_url"), row.get("forensics_title"),
                            str(row.get("forensics_body"))[:200], str(row.get("forensics_console"))[:400],
                        )
                except BaseException:
                    pass
                if _is_playwright_window_closed_error(exc):
                    row["playback_window_closed"] = True
                results.append(row)
                keep_open = _coerce_keep_browser_on_failure(scenario.get("keep_browser_on_failure")) or keep_open_on_missing
                close_browser_when_done = not keep_open
            finally:
                if can_reuse and _is_context_alive(context):
                    _playback_shared_context = context
                    _playback_shared_profile_dir = playback_profile_dir
                    close_browser_when_done = False
                elif close_browser_when_done:
                    if _playback_shared_context == context:
                        _playback_shared_context = None
                        _playback_shared_profile_dir = None
                    shut_ok = False
                    try:
                        shut_ok = await _shutdown_playback_chromium(
                            page_holder[0],
                            context,
                            browser,
                            pb=get_playback_file_logger(),
                            scenario_finished_ok=scenario_reached_end,
                            playback_profile_dir=playback_profile_dir,
                        )
                    except Exception as shutdown_exc:
                        get_playback_file_logger().exception(
                            "خطای غیرمنتظره حین بستن مرورگر پخش (پاسخ به UI ادامه می‌یابد): %s",
                            shutdown_exc,
                        )
                        shut_ok = False
                    finally:
                        if playback_profile_dir is not None:
                            await asyncio.sleep(0.25)
                            shutil.rmtree(playback_profile_dir, ignore_errors=True)
                    browser_close_failed = not shut_ok
                    if not shut_ok:
                        browser_left_open = True
                else:
                    browser_left_open = True
                    log.info(
                        "مرورگر پس از خطا باز ماند (keep_browser_on_failure=true)؛ بعد از اجرا دستی ببند یا در سناریو false بگذار.",
                    )

            if scenario_reached_end:
                await notify_finished()

            out: dict[str, Any] = {
                "results": results,
                "playback_bale_connect_sent": site_ready_sent,
                "playback_bale_finished_sent": finished_sent,
                "playback_cancelled": any(bool(r.get("cancelled")) for r in results),
                "playback_window_closed": any(bool(r.get("playback_window_closed")) for r in results),
                "playback_browser_left_open": browser_left_open,
                "playback_browser_close_failed": bool(
                    close_browser_when_done and browser_close_failed
                ),
                "playback_bale_otp_config_missing": bool(
                    (not bale_otp_creds)
                    and any(bool(s.get("sms_otp") or s.get("sms_otp_fill")) for s in steps_all)
                ),
            }
            out["playback_completed_ok"] = (
                scenario_reached_end
                and len(results) > 0
                and all(bool(r.get("ok")) for r in results)
                and not out["playback_cancelled"]
            )
            if browser_left_open:
                if out["playback_completed_ok"] and bool(out.get("playback_browser_close_failed")):
                    out["playback_note"] = (
                        "سناریو با موفقیت تمام شد اما سرور نتوانست پنجرهٔ اتوماسیون را قطعی ببند؛ اگر هنوز باز است خودت ببند."
                    )
                elif out["playback_window_closed"]:
                    out["playback_note"] = (
                        "پنجرهٔ مرورگر در میانهٔ اجرا بسته شد یا از دست رفت (یا خودت زدی بست)؛ سناریو از نظر برنامه ناتمام است. برای اجرای کامل همان پنجره را تا پیام «کار انجام شد» باز نگه دار."
                    )
                else:
                    out["playback_note"] = (
                        "پنجرهٔ مرورگر باز مانده تا صفحهٔ خطا را ببینی؛ هر اجرای بعدی باز یک پنجرهٔ جدید باز می‌کند مگر قبلی‌ها را خودت ببندی."
                    )
            get_playback_file_logger().info(
                "پایان _playback_body | completed_ok=%s | ok_flags=%s | cancelled=%s | browser_left_open=%s | close_browser=%s",
                out.get("playback_completed_ok"),
                [bool(r.get("ok")) for r in results],
                out["playback_cancelled"],
                browser_left_open,
                close_browser_when_done,
            )
            return out
        finally:
            if on_playback_registry_teardown:
                try:
                    on_playback_registry_teardown()
                except Exception:
                    log.exception("خطا در teardown رجیستری توقف پخش")

    disconnect_recovered = False
    task = asyncio.create_task(_playback_body())
    try:
        out = await asyncio.shield(task)
    except asyncio.CancelledError:
        disconnect_recovered = True
        get_playback_file_logger().warning(
            "CancelledError روی await shield — معمولاً قطع HTTP یا لغو تسک میزبان؛ در انتظار اتمام تسک پخش…",
        )
        log.info(
            "اتصال کلاینت یا تسک میزبان برای درخواست پخش قطع شد؛ اجرای پخش تا پایان در پس‌زمینه کامل می‌شود (تب را باز بگذار).",
        )
        out = await task
    if disconnect_recovered:
        out["playback_survived_client_disconnect"] = True
    out.setdefault("playback_log_file", str(get_playback_log_file_path()))
    return out


async def run_playback_queue(
    queue_items: list[dict[str, Any]],
    *,
    pause_between_steps_ms: float | None = None,
    playback_health_refresh_interval_s: float | None = None,
    pause_between_scenarios_s: float = 2.5,
    bale_send: Callable[[str], Awaitable[None]] | None = None,
    cancel_check: Callable[[], Awaitable[bool]] | None = None,
    on_playback_registry_teardown: Callable[[], None] | None = None,
    bale_otp_creds: tuple[str, str] | None = None,
    stop_on_first_error: bool = True,
    turbo_mode: bool | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """
    چند سناریو پشت‌سرهم در یک مرورگر؛ بین هر کدام مرورگر بسته نمی‌شود و به نشانی شروع بعدی می‌رود.
    """
    if not queue_items:
        return {
            "results": [],
            "queue_completed_ok": False,
            "playback_completed_ok": False,
            "playback_log_file": str(get_playback_log_file_path()),
            "error": "صف خالی است.",
        }

    turbo = is_turbo_mode(None, turbo_mode)
    if turbo and pause_between_scenarios_s == 2.5:
        pause_between_scenarios_s = 0.0

    pb = get_playback_file_logger()
    pb.info(
        "=== شروع run_playback_queue | turbo=%s | items=%s | pause_between_scenarios_s=%s",
        turbo,
        len(queue_items),
        pause_between_scenarios_s,
    )

    async def _queue_body() -> dict[str, Any]:
        playback_profile_dir: Path | None = None
        pw = await launch_playwright()
        playback_profile_dir = Path(tempfile.mkdtemp(prefix="swa_queue_"))
        sw_policy = (
            "block"
            if any(isinstance(it, dict) and it.get("block_service_workers") for it in queue_items)
            else None
        )
        ctx_kwargs: dict[str, Any] = {
            "headless": False,
            "locale": "fa-IR",
            "viewport": {"width": 1280, "height": 800},
            "args": CHROMIUM_TURBO_ARGS,
        }
        if sw_policy:
            ctx_kwargs["service_workers"] = sw_policy
        try:
            context = await pw.chromium.launch_persistent_context(
                str(playback_profile_dir.resolve()),
                **ctx_kwargs,
            )
        except BaseException:
            shutil.rmtree(playback_profile_dir, ignore_errors=True)
            raise

        browser = context.browser
        _pp = context.pages
        page_holder: list[Any] = [_pp[0]] if _pp else [await context.new_page()]

        queue_results: list[dict[str, Any]] = []
        queue_ok = True
        cancelled = False
        site_ready_sent = False

        async def notify_site_ready_once() -> None:
            nonlocal site_ready_sent
            if not bale_send or site_ready_sent:
                return
            site_ready_sent = True
            try:
                await bale_send("صف سناریو شروع شد؛ مرورگر باز است و سناریوها نوبتی اجرا می‌شوند.")
            except Exception:
                log.warning("ارسال پیام بله شروع صف ناموفق", exc_info=True)

        try:
            for qi, item in enumerate(queue_items):
                if cancel_check and await cancel_check():
                    cancelled = True
                    queue_results.append(
                        {
                            "queue_index": qi,
                            "label": item.get("label") or item.get("script_id") or f"سناریو {qi + 1}",
                            "ok": False,
                            "cancelled": True,
                            "error": "توقف توسط کاربر",
                        }
                    )
                    queue_ok = False
                    break

                script_id = str(item.get("script_id") or "").strip()
                if not script_id:
                    queue_results.append(
                        {
                            "queue_index": qi,
                            "label": item.get("label") or f"سناریو {qi + 1}",
                            "ok": False,
                            "error": "شناسهٔ سناریو خالی است",
                        }
                    )
                    queue_ok = False
                    if stop_on_first_error:
                        break
                    continue

                scenario = load_script(script_id)
                if not scenario:
                    queue_results.append(
                        {
                            "queue_index": qi,
                            "script_id": script_id,
                            "label": item.get("label") or script_id,
                            "ok": False,
                            "error": f"فایل سناریو «{script_id}» پیدا نشد",
                        }
                    )
                    queue_ok = False
                    if stop_on_first_error:
                        break
                    continue

                meta = scenario.get("meta") if isinstance(scenario.get("meta"), dict) else {}
                label = str(item.get("label") or meta.get("name") or script_id).strip()
                item_start = str(item.get("start_url") or "").strip() or None
                globals_default = dict(scenario.get("default_values") or {})
                runs = item.get("runs") if isinstance(item.get("runs"), list) else scenario.get("runs")
                if not runs:
                    runs = [{"label": "تک‌بار", "values": {}}]

                q_turbo, q_interval, q_keep = _resolve_turbo_params(scenario)
                q_timeout_s = float(scenario.get("turbo_timeout_s") or scenario.get("turbo_timeout_seconds") or 1800.0)
                turbo = is_turbo_mode(scenario, turbo_mode) or q_turbo
                extra_step_pause = 0.0 if turbo else _resolve_extra_step_pause_ms(scenario, pause_between_steps_ms)
                health_refresh_s = (
                    0.0
                    if turbo
                    else _resolve_playback_health_refresh_s(
                        scenario,
                        playback_health_refresh_interval_s,
                    )
                )
                sms_otp_wait_s = _resolve_sms_otp_wait_seconds_from_scenario(scenario)
                nav_max, nav_pause, nav_goto_ms = _playback_resolve_nav_retry_params(scenario)
                steps_all = playback_steps_with_entry(scenario, playback_start_url=item_start)
                show_marker = False if turbo else _scenario_show_visual_marker(scenario)
                captcha_enabled = (
                    bool(scenario.get("auto_solve_captcha"))
                    if turbo
                    else scenario.get("auto_solve_captcha", DEFAULT_AUTO_SOLVE_CAPTCHA)
                )

                page_holder[0] = _playback_resolve_active_page(context, page_holder[0])
                page = page_holder[0]

                if qi > 0:
                    entry = _resolve_scenario_entry_url(scenario, item_start, globals_default)
                    if entry:
                        handoff_log: list[str] = []
                        try:
                            await _playback_handoff_to_entry(
                                page,
                                entry,
                                goto_timeout_ms=nav_goto_ms,
                                goto_retry_max_attempts=nav_max,
                                goto_retry_pause_s=nav_pause,
                                cancel_check=cancel_check,
                                progress_log=handoff_log,
                            )
                        except BaseException as exc:
                            queue_results.append(
                                {
                                    "queue_index": qi,
                                    "script_id": script_id,
                                    "label": label,
                                    "ok": False,
                                    "error": f"انتقال به شروع سناریو ناموفق: {exc}",
                                    "log": handoff_log,
                                }
                            )
                            queue_ok = False
                            if stop_on_first_error:
                                break
                            continue

                scenario_runs_out: list[dict[str, Any]] = []
                scenario_ok = True
                try:
                    for ri, run_one in enumerate(runs):
                        run_label = run_one.get("label") or f"اجرای {ri + 1}"
                        task_vals = dict(run_one.get("values") or {})
                        log_lines = await run_scenario_steps(
                            browser,
                            context,
                            page_holder,
                            steps_all,
                            task_vals,
                            scenario=scenario,
                            defaults=globals_default,
                            extra_pause_after_step_ms=extra_step_pause,
                            show_visual_marker=show_marker,
                            goto_retry_max_attempts=nav_max,
                            goto_retry_pause_s=nav_pause,
                            goto_per_attempt_timeout_ms=nav_goto_ms,
                            health_refresh_interval_s=health_refresh_s,
                            bale_otp_creds=bale_otp_creds,
                            sms_otp_max_wait_s=sms_otp_wait_s,
                            on_first_page_ready=notify_site_ready_once if qi == 0 and ri == 0 else None,
                            cancel_check=cancel_check,
                            auto_solve_captcha=captcha_enabled,
                            turbo=turbo,
                            turbo_refresh=q_turbo,
                            refresh_interval_ms=q_interval,
                            keep_open_on_missing=q_keep,
                            turbo_timeout_s=q_timeout_s,
                        )
                        scenario_runs_out.append({"label": run_label, "ok": True, "log": log_lines})
                        _pb2_raw = scenario.get("pause_between_tasks_ms")
                        pause_between = 0.0 if turbo else (float(_pb2_raw) if _pb2_raw is not None else 1200.0)
                        if pause_between > 0 and ri < len(runs) - 1:
                            await _sleep_cancellable(pause_between / 1000, cancel_check)
                except PlaybackCancelled:
                    cancelled = True
                    scenario_ok = False
                    scenario_runs_out.append(
                        {"label": "توقف", "ok": False, "cancelled": True, "error": "توقف توسط کاربر"},
                    )
                except PlaybackSessionLost as exc:
                    scenario_ok = False
                    scenario_runs_out.append(
                        {
                            "label": "مرورگر بسته شد",
                            "ok": False,
                            "error": str(exc),
                            "playback_window_closed": True,
                        }
                    )
                except Exception as exc:
                    scenario_ok = False
                    scenario_runs_out.append(
                        {"label": "خطا", "ok": False, "error": str(exc)},
                    )

                queue_results.append(
                    {
                        "queue_index": qi,
                        "script_id": script_id,
                        "label": label,
                        "ok": scenario_ok,
                        "runs": scenario_runs_out,
                    }
                )
                if not scenario_ok:
                    queue_ok = False
                    if stop_on_first_error:
                        break

                eff_pause_scen = 0.0 if turbo else float(pause_between_scenarios_s)
                if qi < len(queue_items) - 1 and eff_pause_scen > 0:
                    await _sleep_cancellable(eff_pause_scen, cancel_check)

            if bale_send and queue_ok:
                try:
                    await bale_send("صف سناریوها تمام شد.")
                except Exception:
                    log.warning("پیام پایان صف به بله ناموفق", exc_info=True)

        finally:
            shut_ok = False
            try:
                shut_ok = await _shutdown_playback_chromium(
                    page_holder[0],
                    context,
                    browser,
                    pb=pb,
                    scenario_finished_ok=queue_ok and not cancelled,
                    playback_profile_dir=playback_profile_dir,
                )
            except Exception:
                pb.exception("بستن مرورگر پس از صف")
            finally:
                if playback_profile_dir is not None:
                    await asyncio.sleep(0.25)
                    shutil.rmtree(playback_profile_dir, ignore_errors=True)

        out = {
            "results": queue_results,
            "queue_completed_ok": queue_ok and not cancelled,
            "playback_completed_ok": queue_ok and not cancelled,
            "playback_cancelled": cancelled,
            "playback_browser_close_failed": not shut_ok,
            "playback_bale_connect_sent": site_ready_sent,
        }
        pb.info(
            "پایان run_playback_queue | completed_ok=%s | items_ok=%s/%s",
            out["queue_completed_ok"],
            sum(1 for r in queue_results if r.get("ok")),
            len(queue_results),
        )
        return out

    task = asyncio.create_task(_queue_body())
    try:
        out = await asyncio.shield(task)
    except asyncio.CancelledError:
        out = await task
        out["playback_survived_client_disconnect"] = True
    finally:
        if on_playback_registry_teardown:
            try:
                on_playback_registry_teardown()
            except Exception:
                log.exception("teardown صف پخش")
    out.setdefault("playback_log_file", str(get_playback_log_file_path()))
    return out


def _playback_resolve_nav_retry_params(
    scenario: dict[str, Any],
) -> tuple[int | None, float, int]:
    """(nav_max, nav_pause_s, nav_goto_ms) از سناریو."""
    raw_nav = scenario.get("nav_retry_max_attempts")
    nav_max: int | None = None
    if raw_nav is not None and raw_nav != "":
        try:
            v = int(float(raw_nav))
            if v > 1:
                nav_max = min(v, 2_147_483_647)
        except Exception:
            nav_max = None
    try:
        nav_pause = float(scenario.get("nav_retry_pause_seconds") or PLAYBACK_NAV_RETRY_PAUSE_S)
    except Exception:
        nav_pause = PLAYBACK_NAV_RETRY_PAUSE_S
    nav_pause = max(1.0, min(nav_pause, 120.0))
    try:
        nav_goto_ms = int(float(scenario.get("nav_retry_goto_timeout_ms") or PLAYBACK_NAV_GOTO_MS_DEFAULT))
    except Exception:
        nav_goto_ms = PLAYBACK_NAV_GOTO_MS_DEFAULT
    nav_goto_ms = max(3000, min(nav_goto_ms, 300_000))
    return nav_max, nav_pause, nav_goto_ms
