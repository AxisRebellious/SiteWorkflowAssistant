"""
updater.py - موتور به‌روزرسانی خودکار، امن و آفلاین برای SiteWorkflowAssistant
تنها با استفاده از کتابخانه‌های استاندارد پایتون (بدون وابستگی خارجی)

توابع کلیدی:
1. load_config(root) -> dict
2. check_update(root, http_get=None) -> dict
3. download_update(zip_url, dest_path, progress_cb=None, min_size=1MB) -> Path
4. verify_stage(stage_dir) -> tuple[bool, str]
5. apply_staged(root, stage_dir) -> dict
6. CLI modes:
   - python updater.py --apply-staged --port PORT
   - python updater.py --full-update --port PORT
   - python updater.py --check
   - python updater.py --self-test
"""

from __future__ import annotations

import argparse
import datetime
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# تنظیمات پیش‌فرض به‌روزرسانی در صورت عدم وجود update_config.json
DEFAULT_CONFIG: dict[str, str] = {
    "source": "github",
    "repo": "",
    "branch": "main",
    "version_url": "",
    "asset_name": "",
}

# حداقل حجم مجاز فایل زیپ به‌روزرسانی (۵۰ کیلوبایت؛ بسته‌های فقط-کد واقعی حدود ۰.۱ مگابایت‌اند).
# فایل‌های کوچک‌تر (صفحات خطا) رد می‌شوند؛ ناقص‌بودن با بررسی سلامت زیپ هم گرفته می‌شود.
MIN_UPDATE_ZIP_SIZE: int = 50 * 1024

# لیست مسیرهایی که هرگز نباید رونویسی شوند تا فعال‌سازی و داده‌های کاربر دست‌نخورده بماند
EXCLUDED_PATHS: set[str] = {
    "runtime",
    "data/license.json",
    "data/logs",
    "data/last_port.txt",
    "admin_data",
    "__pycache__",
    "data/update_stage",
    "data/update_stage.zip",
    ".git",
    ".venv",
    ".venv_new",
}

# فایل‌های الزامی که وجودشان در بسته استیج ضروری است
REQUIRED_STAGE_FILES: list[Path] = [
    Path("main.py"),
    Path("license.py"),
    Path("static") / "license.html",
]


def _get_log_file(root: Path) -> Path:
    """مسیر فایل لاگ اختصاصی updater را برمی‌گرداند و پوشه آن را در صورت نیاز می‌سازد."""
    log_dir = root / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "updater.log"


def log_msg(root: Path, message: str, level: str = "INFO") -> None:
    """ثبت پیام فارسی در کنسول و ضمیمه کردن به فایل data/logs/updater.log."""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{now_str}] [{level}] {message}"
    print(formatted, flush=True)
    try:
        log_file = _get_log_file(root)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(formatted + "\n")
    except Exception:
        pass


def load_config(root: str | Path) -> dict[str, Any]:
    """
    خواندن تنظیمات از update_config.json.
    در صورت نبود فایل، فایل پیش‌فرض را ایجاد کرده و بازمی‌گرداند.
    خروجی: {source, repo, branch, version_url, asset_name}
    """
    root_path = Path(root).resolve()
    config_file = root_path / "update_config.json"

    if not config_file.exists():
        cfg = dict(DEFAULT_CONFIG)
        try:
            config_file.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            raise RuntimeError(f"خطا در ساخت فایل پیش‌فرض update_config.json: {exc}") from exc
        return cfg

    try:
        content = config_file.read_text(encoding="utf-8")
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("ساختار فایل تنظیمات باید یک شیء JSON باشد.")
    except Exception as exc:
        raise RuntimeError(f"فایل تنظیمات به‌روزرسانی (update_config.json) نامعتبر است: {exc}") from exc

    return {
        "source": data.get("source", DEFAULT_CONFIG["source"]),
        "repo": data.get("repo", DEFAULT_CONFIG["repo"]),
        "branch": data.get("branch", DEFAULT_CONFIG["branch"]),
        "version_url": data.get("version_url", DEFAULT_CONFIG["version_url"]),
        "asset_name": data.get("asset_name", DEFAULT_CONFIG["asset_name"]),
    }


def _parse_version(v: str) -> tuple[int | str, ...]:
    """تجزیه رشته نسخه به اجزای عددی و متنی جهت مقایسه ترتیبی."""
    cleaned = v.strip().lstrip("vV")
    tokens = re.findall(r"\d+|\D+", cleaned)
    parts: list[int | str] = []
    for tok in tokens:
        if tok.isdigit():
            parts.append(int(tok))
        else:
            cleaned_tok = tok.strip(".-_")
            if cleaned_tok:
                parts.append(cleaned_tok)
    return tuple(parts)


def _is_version_newer(latest: str, current: str) -> bool:
    """بررسی اینکه آیا نسخه سرور (latest) جدیدتر از نسخه محلی (current) است یا خیر."""
    if not latest:
        return False
    if not current or current == "0.0.0":
        return True
    try:
        p_lat = _parse_version(latest)
        p_cur = _parse_version(current)
        if all(isinstance(x, int) for x in p_lat) and all(isinstance(x, int) for x in p_cur):
            max_len = max(len(p_lat), len(p_cur))
            p_lat_padded = p_lat + (0,) * (max_len - len(p_lat))
            p_cur_padded = p_cur + (0,) * (max_len - len(p_cur))
            return p_lat_padded > p_cur_padded
        return p_lat > p_cur
    except Exception:
        return latest != current


def _doh_resolve_ips(host: str) -> list[str]:
    """رزولو هاست از طریق DNS رمزنگاری‌شده (برای شبکه‌هایی که DNS محلی را دست‌کاری می‌کنند)."""
    import urllib.parse

    ips: list[str] = []
    queries = [
        "https://dns.google/resolve?name=" + urllib.parse.quote(host) + "&type=A",
        "https://cloudflare-dns.com/dns-query?name=" + urllib.parse.quote(host) + "&type=A",
    ]
    for q in queries:
        try:
            req = urllib.request.Request(
                q,
                headers={"accept": "application/dns-json", "User-Agent": "SiteWorkflowAssistant-Updater/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for ans in data.get("Answer") or []:
                if ans.get("type") == 1 and ans.get("data"):
                    ip = str(ans["data"]).strip()
                    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip) and ip not in ips:
                        ips.append(ip)
            if ips:
                return ips
        except Exception:
            continue
    return ips


class _SniPinnedHTTPSConnection(http.client.HTTPSConnection):
    """اتصال HTTPS به IP ازپیش‌رزولوشده با حفظ hostname برای SNI و اعتبارسنجی گواهی."""

    def connect(self) -> None:
        import socket as _socket

        pin_ip = getattr(self, "_pin_ip", None)
        dest = (pin_ip, self.port) if pin_ip else (self.host, self.port)
        self.sock = _socket.create_connection(dest, self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _resilient_urlopen(url_or_req: Any, timeout: int = 15) -> Any:
    """urlopen با fallback خودکار روی DoH. خروجی مثل پاسخ urlopen (کانتکست‌منجر + read/headers)."""
    import http.client
    import urllib.parse

    try:
        return urllib.request.urlopen(url_or_req, timeout=timeout)
    except Exception as first_exc:
        url = url_or_req.full_url if hasattr(url_or_req, "full_url") else str(url_or_req)
        try:
            host = urllib.parse.urlsplit(url).hostname or ""
        except Exception:
            raise first_exc
        if not host:
            raise first_exc
        ips = _doh_resolve_ips(host)
        if not ips:
            raise first_exc
        last_exc: Exception = first_exc
        for _attempt in range(6):
            try:
                return _pinned_open(url_or_req, url, ips, timeout)
            except Exception as exc:
                last_exc = exc
                continue
        raise last_exc


def _pinned_open(url_or_req: Any, url: str, ips: list[str], timeout: int) -> Any:
    """باز کردن URL با اتصال مستقیم به یکی از IPها + دنبال‌کردن دستی ریدایرکت (تا ۵ پرش)."""
    import http.client
    import socket
    import urllib.parse

    if hasattr(url_or_req, "header_items"):
        method = url_or_req.get_method()
        data = url_or_req.data
        headers = dict(url_or_req.header_items())
        if "User-agent" not in headers and "User-Agent" not in headers:
            headers["User-Agent"] = "SiteWorkflowAssistant-Updater/1.0"
    else:
        method, data, headers = "GET", None, {"User-Agent": "SiteWorkflowAssistant-Updater/1.0"}

    cur_url = url
    last_exc: Exception | None = None
    for _hop in range(6):
        parts = urllib.parse.urlsplit(cur_url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        port = parts.port or (443 if parts.scheme == "https" else 80)
        host = parts.hostname or ""

        # دریافت آدرس‌های IP هاست فعلی این پرش (سامانه محلی + DoH برای عبور از فیلترینگ DNS)
        cur_ips: list[str] = []
        if host:
            try:
                for ai in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
                    ip_str = ai[4][0]
                    if ip_str not in cur_ips:
                        cur_ips.append(ip_str)
            except Exception:
                pass
            for doh_ip in _doh_resolve_ips(host):
                if doh_ip not in cur_ips:
                    cur_ips.append(doh_ip)
        if not cur_ips:
            cur_ips = ips

        ok = False
        for ip in cur_ips:
            try:
                if parts.scheme == "https":
                    conn: Any = _SniPinnedHTTPSConnection(host, port, timeout=timeout)
                    conn._pin_ip = ip
                else:
                    conn = http.client.HTTPConnection(ip, port, timeout=timeout)
                conn.request(method, path, body=data, headers=headers)
                resp = conn.getresponse()
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.getheader("Location") or ""
                    try:
                        resp.close()
                    except Exception:
                        pass
                    try:
                        conn.close()
                    except Exception:
                        pass
                    if not loc:
                        raise RuntimeError(f"ریدایرکت بدون مقصد ({resp.status})")
                    cur_url = urllib.parse.urljoin(cur_url, loc)
                    if resp.status == 303:
                        method, data = "GET", None
                    ok = True
                    break
                if resp.status >= 400:
                    body = ""
                    try:
                        body = resp.read(500).decode("utf-8", errors="replace")
                    except Exception:
                        pass
                    try:
                        conn.close()
                    except Exception:
                        pass
                    raise RuntimeError(f"خطای HTTP {resp.status}: {body[:200]}")
                return resp
            except Exception as exc:
                last_exc = exc
                try:
                    conn.close()
                except Exception:
                    pass
                continue
        if ok:
            continue
        break
    raise last_exc if last_exc else RuntimeError("اتصال به سرور ممکن نشد.")


def _default_http_get(url: str) -> bytes:
    """دریافت داده‌های وب از طریق کتابخانه استاندارد urllib با مهلت ۱۵ ثانیه."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "SiteWorkflowAssistant-Updater/1.0"},
    )
    with _resilient_urlopen(req, timeout=15) as resp:
        return resp.read()


def check_update(
    root: str | Path,
    http_get: Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
    """
    بررسی وجود نسخه جدید با مقایسه version.json محلی و فایل نسخه از سرور.
    پشتیبانی از تزریق http_get برای محیط‌های تستی و offline-safe.
    خروجی: {current, latest, available: bool, notes, zip_url}
    """
    root_path = Path(root).resolve()
    config = load_config(root_path)

    # خواندن نسخه فعلی برنامه
    current = "0.0.0"
    version_file = root_path / "version.json"
    if version_file.is_file():
        try:
            v_data = json.loads(version_file.read_text(encoding="utf-8"))
            if isinstance(v_data, dict):
                current = str(v_data.get("version", "0.0.0")).strip()
        except Exception:
            current = "0.0.0"

    # یافتن آدرس دریافت فایل اطلاعات نسخه سرور
    remote_url = (config.get("version_url") or "").strip()
    if not remote_url:
        source = (config.get("source") or "").strip().lower()
        repo = (config.get("repo") or "").strip()
        branch = (config.get("branch") or "main").strip() or "main"
        if source == "github" and repo:
            remote_url = f"https://raw.githubusercontent.com/{repo}/{branch}/version.json"
        elif http_get is not None:
            remote_url = "https://mock.test/version.json"
        else:
            return {
                "current": current,
                "latest": current,
                "available": False,
                "notes": "سرور به‌روزرسانی تنظیم نشده است.",
                "zip_url": "",
            }

    # فراخوانی تابع fetcher (پیش‌فرض یا تزریقی)
    fetcher = http_get if http_get is not None else _default_http_get
    try:
        raw_bytes = fetcher(remote_url)
    except Exception as exc:
        raise RuntimeError(f"خطا در برقراری ارتباط با سرور به‌روزرسانی و دریافت اطلاعات نسخه: {exc}") from exc

    try:
        remote_info = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(remote_info, dict):
            raise ValueError("داده‌های دریافتی از سرور حاوی شیء معتبر JSON نیستند.")
    except Exception as exc:
        raise RuntimeError(f"قالب اطلاعات نسخه دریافتی از سرور نامعتبر است: {exc}") from exc

    latest = str(remote_info.get("version", "")).strip()
    if not latest:
        raise RuntimeError("اطلاعات نسخه دریافتی از سرور فاقد فیلد version است.")

    notes = str(remote_info.get("notes", "")).strip()

    # تعیین آدرس بسته دانلود زیپ
    zip_url = str(remote_info.get("zip_url") or remote_info.get("download_url") or "").strip()
    if not zip_url:
        repo = (config.get("repo") or "").strip()
        asset_name = (config.get("asset_name") or "").strip()
        if repo and asset_name:
            tag = f"v{latest}" if not latest.startswith("v") else latest
            zip_url = f"https://github.com/{repo}/releases/download/{tag}/{asset_name}"

    available = _is_version_newer(latest, current)

    return {
        "current": current,
        "latest": latest,
        "available": available,
        "notes": notes,
        "zip_url": zip_url,
    }


def download_update(
    zip_url: str,
    dest_path: str | Path,
    progress_cb: Callable[[int, int | None], None] | None = None,
    min_size: int = MIN_UPDATE_ZIP_SIZE,
) -> Path:
    """
    دانلود بسته به‌روزرسانی به صورت جریانی (Streamed) با مهلت ۱۲۰ ثانیه و گارد حداقل حجم مجاز.
    دانلود ابتدا در یک فایل موقت .part انجام شده و پس از اعتبارسنجی جابجا می‌شود.
    """
    if not zip_url:
        raise RuntimeError("آدرس دانلود فایل به‌روزرسانی (zip_url) خالی است.")

    dest = Path(dest_path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    # تبدیل مسیرهای محلی به آدرس URI جهت سازگاری با urllib
    req_url = zip_url
    if not (req_url.startswith("http://") or req_url.startswith("https://") or req_url.startswith("file://")):
        local_p = Path(zip_url)
        if local_p.exists():
            req_url = local_p.resolve().as_uri()

    part_file = dest.with_name(dest.name + ".part")
    if part_file.exists():
        part_file.unlink(missing_ok=True)

    req = urllib.request.Request(
        req_url,
        headers={"User-Agent": "SiteWorkflowAssistant-Updater/1.0"},
    )

    try:
        with _resilient_urlopen(req, timeout=120) as resp, open(part_file, "wb") as out_f:
            total_hdr = resp.headers.get("Content-Length")
            total_bytes = int(total_hdr) if total_hdr and total_hdr.isdigit() else None
            downloaded = 0
            chunk_size = 64 * 1024

            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                out_f.write(chunk)
                downloaded += len(chunk)
                if progress_cb is not None:
                    try:
                        progress_cb(downloaded, total_bytes)
                    except Exception:
                        pass
    except Exception as exc:
        if part_file.exists():
            part_file.unlink(missing_ok=True)
        raise RuntimeError(f"خطا در دریافت و دانلود فایل به‌روزرسانی: {exc}") from exc

    # بررسی گارد حداقل اندازه
    final_size = part_file.stat().st_size
    if final_size < min_size:
        part_file.unlink(missing_ok=True)
        raise RuntimeError(
            f"اندازه فایل دانلود شده ({final_size} بایت) کمتر از حداقل مجاز ({min_size} بایت) است. امکان دانلود ناقص وجود دارد."
        )

    # بررسی سلامت و ساختار زیپ
    if not zipfile.is_zipfile(part_file):
        part_file.unlink(missing_ok=True)
        raise RuntimeError("فایل دانلود شده یک فایل فشرده (ZIP) معتبر نیست.")

    # انتقال به مقصد نهایی
    if dest.exists():
        dest.unlink(missing_ok=True)
    part_file.replace(dest)

    return dest


def find_stage_root(stage_dir: Path) -> Path:
    """
    بررسی پوشه استیج و یافتن ریشه واقعی کدها؛
    در صورتی که فایل‌های زیپ در یک زیرپوشه سطح اول قرار گرفته باشند، پوشه داخلی را شناسایی می‌کند.
    """
    if (stage_dir / "main.py").exists():
        return stage_dir
    if stage_dir.is_dir():
        for item in stage_dir.iterdir():
            if item.is_dir() and (item / "main.py").exists():
                return item
    return stage_dir


def verify_stage(stage_dir: str | Path) -> tuple[bool, str]:
    """
    اعتبارسنجی پوشه استیج به‌روزرسانی.
    الزام وجود فایل‌های: main.py و license.py و static/license.html
    خروجی: (bool, reason)
    """
    stage = Path(stage_dir).resolve()
    if not stage.exists() or not stage.is_dir():
        return False, "پوشه استیج به‌روزرسانی وجود ندارد یا یک دایرکتوری معتبر نیست."

    stage_root = find_stage_root(stage)
    for req in REQUIRED_STAGE_FILES:
        target = stage_root / req
        if not target.is_file():
            return False, f"فایل الزامی «{req.as_posix()}» در بسته به‌روزرسانی یافت نشد."

    return True, "بسته به‌روزرسانی معتبر است و ساختار فایل‌های اصلی تأیید شد."


def is_excluded(rel_path: Path) -> bool:
    """
    بررسی اینکه آیا مسیر در لیست استثناهای ممنوعه قرار دارد یا خیر.
    استثناها:
    - runtime (پایتون پرتابل و مرورگرهای آفلاین مشتری)
    - data/license.json (لایسنس فعال‌سازی مشتری - هرگز رونویسی نمی‌شود)
    - data/logs (لاگ‌های سیستم)
    - data/last_port.txt (پورت رزرو شده)
    - admin_data (کلیدهای خصوصی و ابزارهای ادمین)
    - __pycache__ (بایت‌کدهای پایتون)
    """
    parts = rel_path.parts
    if not parts:
        return False
    if "__pycache__" in parts:
        return True

    posix_path = rel_path.as_posix().lower()

    # رانتایم مشتری
    if posix_path == "runtime" or posix_path.startswith("runtime/"):
        return True

    # لایسنس فعال مشتری
    if posix_path == "data/license.json":
        return True

    # لاگ‌های سامانه
    if posix_path == "data/logs" or posix_path.startswith("data/logs/"):
        return True

    # پورت آخر
    if posix_path == "data/last_port.txt":
        return True

    # داده‌های محرمانه ادمین
    if posix_path == "admin_data" or posix_path.startswith("admin_data/"):
        return True

    # فایل‌های استیج موقت خود به‌روزرسان
    if posix_path == "data/update_stage" or posix_path.startswith("data/update_stage/"):
        return True
    if posix_path == "data/update_stage.zip":
        return True

    # فایل‌های توسعه محلی
    if posix_path == ".git" or posix_path.startswith(".git/"):
        return True
    if posix_path == ".venv" or posix_path.startswith(".venv/"):
        return True
    if posix_path == ".venv_new" or posix_path.startswith(".venv_new/"):
        return True

    return False


def apply_staged(root: str | Path, stage_dir: str | Path) -> dict[str, Any]:
    """
    اعمال فایل‌های استخراج‌شده روی پوشه ریشه پروژه به جز فایل‌های مستثنی شده.
    سپس نوشتن version.json در ریشه.
    خروجی: {updated_files: int, version: str}
    """
    root_path = Path(root).resolve()
    stage_path = Path(stage_dir).resolve()

    valid, reason = verify_stage(stage_path)
    if not valid:
        raise RuntimeError(f"بسته استیج فاقد صلاحیت برای اعمال است: {reason}")

    stage_root = find_stage_root(stage_path)
    updated_files = 0

    # خواندن اطلاعات نسخه جدید از استیج
    new_version = "unknown"
    stage_ver_file = stage_root / "version.json"
    ver_payload: dict[str, Any] = {}
    if stage_ver_file.is_file():
        try:
            ver_payload = json.loads(stage_ver_file.read_text(encoding="utf-8"))
            if isinstance(ver_payload, dict):
                new_version = str(ver_payload.get("version", "unknown")).strip()
        except Exception:
            pass

    try:
        for cur_dir, dirnames, filenames in os.walk(stage_root):
            cur_path = Path(cur_dir)
            rel_dir = cur_path.relative_to(stage_root)

            # حذف پوشه‌های مستثنی از فرآیند پیمایش
            dirnames[:] = [
                d for d in dirnames
                if not is_excluded(rel_dir / d if rel_dir != Path(".") else Path(d))
            ]

            for fname in filenames:
                file_rel = (rel_dir / fname) if rel_dir != Path(".") else Path(fname)
                if is_excluded(file_rel):
                    continue
                # فایل version.json در انتهای کار به شکل اتمیک ذخیره خواهد شد
                if file_rel == Path("version.json"):
                    continue

                src_file = cur_path / fname
                dst_file = root_path / file_rel

                dst_file.parent.mkdir(parents=True, exist_ok=True)
                if dst_file.exists():
                    try:
                        os.chmod(dst_file, 0o777)
                    except Exception:
                        pass
                shutil.copy2(src_file, dst_file)
                updated_files += 1

        # نوشتن فایل نهایی version.json در ریشه
        dst_ver_file = root_path / "version.json"
        if ver_payload:
            dst_ver_file.write_text(
                json.dumps(ver_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        elif new_version != "unknown":
            dst_ver_file.write_text(
                json.dumps({"version": new_version}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        updated_files += 1

    except Exception as exc:
        raise RuntimeError(f"خطا در حین اعمال و کپی فایل‌های به‌روزرسانی: {exc}") from exc

    return {"updated_files": updated_files, "version": new_version}


def is_port_free(host: str, port: int) -> bool:
    """بررسی اینکه آیا پورت شبکه آزاد است و برنامه‌ای روی آن فعال نیست."""
    if port <= 0:
        return True

    # ۱. تلاش برای اتصال سوکت
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        s.connect((host, port))
        s.close()
        return False
    except (OSError, ConnectionRefusedError):
        pass
    finally:
        try:
            s.close()
        except Exception:
            pass

    # ۲. تلاش برای Bind
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        s.close()
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def wait_port_free(host: str, port: int, timeout: float = 40.0, step: float = 1.0) -> bool:
    """انتظار تا زمان آزاد شدن پورت تا سقف زمانی تعیین شده."""
    if port <= 0:
        return True
    start = time.time()
    while time.time() - start < timeout:
        if is_port_free(host, port):
            return True
        time.sleep(step)
    return is_port_free(host, port)


def launch_detached(root: Path) -> subprocess.Popen:
    """
    راه‌اندازی مستقل و جداگانه لانچر (launch.py) با استفاده از runtime/python/python.exe
    بدون باز شدن پنجره ترمینال و با قابلیت بقا پس از مرگ فرآیند والد.
    """
    runtime_py = root / "runtime" / "python" / "python.exe"
    if runtime_py.is_file():
        py_bin = str(runtime_py)
    else:
        py_bin = sys.executable

    launch_script = root / "launch.py"
    if not launch_script.is_file():
        raise RuntimeError(f"فایل launch.py در مسیر «{root}» یافت نشد.")

    cmd = [py_bin, str(launch_script)]

    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(root / "runtime" / "browsers")

    flags = 0
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

    proc = subprocess.Popen(
        cmd,
        cwd=str(root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )
    return proc


def run_apply_staged_cli(root: Path, port: int, stage_dir: Path | None = None) -> None:
    """
    حالت CLI:
    python updater.py --apply-staged --port PORT
    صبر تا آزاد شدن پورت (تا ۴۰ ثانیه)، اعتبارسنجی استیج، اعمال فایل‌ها و راه‌اندازی detached لانچر.
    """
    log_msg(root, f"شروع عملیات اعمال استیج (پورت: {port})...")

    if port > 0:
        log_msg(root, f"در حال انتظار برای آزاد شدن پورت {port} (تا ۴۰ ثانیه)...")
        if not wait_port_free("127.0.0.1", port, timeout=40.0):
            err = f"پورت {port} پس از ۴۰ ثانیه آزاد نشد. فرآیند قبلی هنوز فعال است."
            log_msg(root, err, level="ERROR")
            raise RuntimeError(err)
        log_msg(root, f"پورت {port} آزاد شد.")

    st_dir = stage_dir or (root / "data" / "update_stage")
    valid, reason = verify_stage(st_dir)
    if not valid:
        err = f"اعتبارسنجی بسته استیج رد شد: {reason}"
        log_msg(root, err, level="ERROR")
        raise RuntimeError(err)

    log_msg(root, f"اعتبارسنجی استیج با موفقیت تأیید گردید: {reason}")
    res = apply_staged(root, st_dir)
    log_msg(root, f"فایل‌های استیج اعمال شدند ({res['updated_files']} فایل). نسخه جدید: {res['version']}")

    # پاک‌سازی استیج موقت
    try:
        shutil.rmtree(st_dir, ignore_errors=True)
    except Exception:
        pass

    log_msg(root, "در حال راه‌اندازی مجدد برنامه به صورت کاملاً مستقل و detached...")
    proc = launch_detached(root)
    log_msg(root, f"برنامه با موفقیت راه‌اندازی شد (PID: {proc.pid}). خروج از به‌روزرسان.")


def run_full_update_cli(root: Path, port: int) -> None:
    """
    حالت CLI:
    python updater.py --full-update --port PORT
    load_config → check_update (اگر نسخه جدید نبود خروج تمیز با کد ۰)
    → download_update → extract → verify_stage
    → انتظار آزادشدن 127.0.0.1:PORT تا ۶۰ ثانیه → apply_staged
    → اجرای detached لانچر → خروج.
    """
    log_msg(root, "شروع فرآیند به‌روزرسانی کامل و خودکار...")

    cfg = load_config(root)
    log_msg(root, f"تنظیمات به‌روزرسانی بارگذاری شد (منبع: {cfg.get('source')})")

    chk = check_update(root)
    log_msg(
        root,
        f"بررسی نسخه: نسخه فعلی={chk['current']}، نسخه جدید سرور={chk['latest']}، نیاز به به‌روزرسانی={chk['available']}",
    )

    if not chk.get("available"):
        log_msg(root, "برنامه در حال حاضر کاملاً به‌روز است. خروج موفق.")
        sys.exit(0)

    zip_url = chk.get("zip_url")
    if not zip_url:
        err = "آدرس دانلود فایل به‌روزرسانی (zip_url) مشخص نیست."
        log_msg(root, err, level="ERROR")
        raise RuntimeError(err)

    dest_zip = root / "data" / "update_stage.zip"
    stage_dir = root / "data" / "update_stage"

    # پیشرفت دانلود
    last_pct = [-1]

    def _progress(down: int, total: int | None) -> None:
        if total and total > 0:
            pct = int((down / total) * 100)
            if pct >= last_pct[0] + 10 or down == total:
                last_pct[0] = pct
                mb_d = down / (1024 * 1024)
                mb_t = total / (1024 * 1024)
                log_msg(root, f"پیشرفت دریافت فایل: {pct}% ({mb_d:.2f}MB از {mb_t:.2f}MB)")
        else:
            mb_d = down / (1024 * 1024)
            if int(mb_d) > last_pct[0]:
                last_pct[0] = int(mb_d)
                log_msg(root, f"پیشرفت دریافت فایل: {mb_d:.2f}MB دریافت شد...")

    log_msg(root, f"شروع دانلود بسته به‌روزرسانی از: {zip_url}")
    download_update(zip_url, dest_zip, progress_cb=_progress)
    log_msg(root, f"دانلود بسته به‌روزرسانی تکمیل شد: {dest_zip}")

    # استخراج فایل زیپ
    if stage_dir.exists():
        shutil.rmtree(stage_dir, ignore_errors=True)
    stage_dir.mkdir(parents=True, exist_ok=True)

    log_msg(root, f"در حال استخراج بسته در پوشه {stage_dir}...")
    try:
        with zipfile.ZipFile(dest_zip, "r") as zf:
            zf.extractall(stage_dir)
    except Exception as exc:
        err = f"خطا در استخراج فایل زیپ به‌روزرسانی: {exc}"
        log_msg(root, err, level="ERROR")
        raise RuntimeError(err) from exc

    log_msg(root, "استخراج فایل‌ها با موفقیت پایان یافت.")

    # اعتبارسنجی ساختار
    valid, reason = verify_stage(stage_dir)
    if not valid:
        err = f"اعتبارسنجی بسته استیج ناموفق بود: {reason}"
        log_msg(root, err, level="ERROR")
        raise RuntimeError(err)
    log_msg(root, f"اعتبارسنجی بسته استیج با موفقیت تأیید شد: {reason}")

    # انتظار برای آزاد شدن پورت سرور تا ۶۰ ثانیه
    if port > 0:
        log_msg(root, f"در حال انتظار برای اتمام کار سرور روی پورت {port} (تا ۶۰ ثانیه)...")
        if not wait_port_free("127.0.0.1", port, timeout=60.0):
            err = f"پورت {port} در مدت ۶۰ ثانیه آزاد نشد. سرور قبلی هنوز در حال فعالیت است."
            log_msg(root, err, level="ERROR")
            raise RuntimeError(err)
        log_msg(root, f"پورت {port} آزاد شد.")

    # اعمال فایل‌ها
    res = apply_staged(root, stage_dir)
    log_msg(root, f"اعمال به‌روزرسانی با موفقیت انجام شد ({res['updated_files']} فایل). نسخه جدید: {res['version']}")

    # پاک‌سازی فایل‌های استیج موقت
    try:
        dest_zip.unlink(missing_ok=True)
        shutil.rmtree(stage_dir, ignore_errors=True)
    except Exception:
        pass

    # راه‌اندازی مجدد بدون پنجره
    log_msg(root, "در حال راه‌اندازی مجدد برنامه به صورت کاملاً مستقل و detached...")
    proc = launch_detached(root)
    log_msg(root, f"برنامه با موفقیت در پس‌زمینه راه‌اندازی شد (PID: {proc.pid}). خروج.")
    sys.exit(0)


def run_check_cli(root: Path) -> None:
    """حالت CLI برای بررسی وضعیت نسخه و چاپ گزارش."""
    chk = check_update(root)
    print("\n--- گزارش وضعیت به‌روزرسانی ---")
    print(f"نسخه فعلی سیستم: {chk['current']}")
    print(f"آخرین نسخه سرور: {chk['latest']}")
    print(f"به‌روزرسانی در دسترس: {'بله' if chk['available'] else 'خیر'}")
    if chk.get("notes"):
        print(f"توضیحات تغییرات: {chk['notes']}")
    if chk.get("zip_url"):
        print(f"آدرس بسته دانلود: {chk['zip_url']}")
    print("----------------------------\n")


def run_self_test(current_root: Path) -> bool:
    """
    آزمون خودکار کامل (Self-Test):
    تست توابع load_config, check_update (با http_get و file://), download_update, verify_stage, apply_staged
    و شبیه‌سازی حالت CLI --full-update با remote محلی.
    """
    import tempfile

    print("\n[تست خودکار] شروع تست جامع updater.py...")

    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        sandbox = tmp_dir / "sandbox_app"
        remote_srv = tmp_dir / "remote_server"
        sandbox.mkdir()
        remote_srv.mkdir()

        # ۱. ساخت ساختار پایه شبیه‌سازی‌شده برنامه در sandbox
        (sandbox / "main.py").write_text("# Old main", encoding="utf-8")
        (sandbox / "license.py").write_text("# License logic", encoding="utf-8")
        (sandbox / "launch.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        (sandbox / "static").mkdir()
        (sandbox / "static" / "license.html").write_text("<html>License Gate</html>", encoding="utf-8")
        (sandbox / "version.json").write_text(
            json.dumps({"version": "13.0", "notes": "نسخه فعلی"}, ensure_ascii=False),
            encoding="utf-8",
        )

        # فایل‌های حیاتی که به هیچ عنوان نباید بازنویسی شوند
        (sandbox / "data").mkdir()
        (sandbox / "data" / "license.json").write_text('{"token": "CUSTOMER_SECRET_LICENSE"}', encoding="utf-8")
        (sandbox / "data" / "logs").mkdir()
        (sandbox / "data" / "logs" / "playback.log").write_text("CRITICAL_USER_LOGS", encoding="utf-8")
        (sandbox / "data" / "last_port.txt").write_text("9876", encoding="utf-8")
        (sandbox / "admin_data").mkdir()
        (sandbox / "admin_data" / "license_privkey.pem").write_text("ADMIN_SECRET_KEY", encoding="utf-8")
        (sandbox / "runtime").mkdir()
        (sandbox / "runtime" / "dummy_browser.txt").write_text("PORTABLE_BROWSER", encoding="utf-8")

        # ۲. تست load_config
        cfg = load_config(sandbox)
        assert cfg["source"] == "github", "خطا در تست load_config: پیش‌فرض نامعتبر"
        assert (sandbox / "update_config.json").exists(), "فایل update_config.json ساخته نشد."
        print("  ✓ تست load_config با موفقیت گذشت.")

        # ۳. ساخت بسته به‌روزرسانی تستی برای سرور فرضی
        stage_content = tmp_dir / "stage_content"
        stage_content.mkdir()
        (stage_content / "main.py").write_text("# NEW 14.0 MAIN CODE", encoding="utf-8")
        (stage_content / "license.py").write_text("# NEW 14.0 LICENSE", encoding="utf-8")
        (stage_content / "static").mkdir()
        (stage_content / "static" / "license.html").write_text("<html>New Gate</html>", encoding="utf-8")
        (stage_content / "version.json").write_text(
            json.dumps({"version": "14.0", "notes": "ویژگی‌های جدید نسخه ۱۴"}, ensure_ascii=False),
            encoding="utf-8",
        )
        # فایل‌هایی که نباید کپی شوند ولی ممکن است در زیپ اشتباهاً وجود داشته باشند
        (stage_content / "admin_data").mkdir()
        (stage_content / "admin_data" / "leaked.txt").write_text("LEAK", encoding="utf-8")
        (stage_content / "data").mkdir()
        (stage_content / "data" / "license.json").write_text('{"token": "HACKED"}', encoding="utf-8")

        # ایجاد یک فایل پرکننده تصادفی (غیرقابل فشرده‌سازی) برای رساندن حجم زیپ به بالای ۱ مگابایت
        padding_file = stage_content / "large_asset.dat"
        padding_file.write_bytes(os.urandom(1024 * 1024 + 10000))

        # ساخت فایل zip
        remote_zip = remote_srv / "update_14.0.zip"
        with zipfile.ZipFile(remote_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for root_w, _, files_w in os.walk(stage_content):
                for f_w in files_w:
                    full_p = Path(root_w) / f_w
                    zf.write(full_p, full_p.relative_to(stage_content))

        assert remote_zip.stat().st_size >= 1024 * 1024, "حجم بسته تستی کمتر از ۱ مگابایت شد."

        # ساخت فایل version.json سرور
        remote_version_file = remote_srv / "version.json"
        remote_version_file.write_text(
            json.dumps({
                "version": "14.0",
                "notes": "نسخه تستی ۱۴",
                "zip_url": remote_zip.resolve().as_uri(),
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        # ۴. تست check_update با تزریق http_get
        def fake_http_get(url: str) -> bytes:
            return json.dumps({
                "version": "14.0",
                "notes": "تزریق تستی",
                "zip_url": "http://fake.local/update.zip",
            }).encode("utf-8")

        chk_fake = check_update(sandbox, http_get=fake_http_get)
        assert chk_fake["available"] is True, "نسخه جدید تشخیص داده نشد."
        assert chk_fake["latest"] == "14.0"
        print("  ✓ تست check_update با تزریق تابع http_get با موفقیت گذشت.")

        # ۵. تنظیم update_config.json برای استفاده از file:// محلی
        cfg["version_url"] = remote_version_file.resolve().as_uri()
        (sandbox / "update_config.json").write_text(json.dumps(cfg), encoding="utf-8")

        chk_local = check_update(sandbox)
        assert chk_local["available"] is True
        assert chk_local["latest"] == "14.0"
        assert chk_local["zip_url"] == remote_zip.resolve().as_uri()
        print("  ✓ تست check_update با آدرس file:// سرور محلی با موفقیت گذشت.")

        # ۶. تست download_update و گارد حداقل حجم مجاز
        small_zip = remote_srv / "small.zip"
        with zipfile.ZipFile(small_zip, "w") as zf:
            zf.writestr("test.txt", "tiny file")

        dest_test_zip = sandbox / "data" / "test_download.zip"
        try:
            download_update(small_zip.resolve().as_uri(), dest_test_zip, min_size=1024 * 1024)
            assert False, "گارد حداقل حجم مجاز عمل نکرد!"
        except RuntimeError as exc:
            assert "کمتر از حداقل مجاز" in str(exc)
            print("  ✓ تست گارد حجم مجاز (رد فایل کمتر از حد) با موفقیت گذشت.")

        # دانلود فایل واقعی با پیشرفت
        progress_calls: list[tuple[int, int | None]] = []
        dest_valid_zip = sandbox / "data" / "valid_download.zip"
        download_update(
            remote_zip.resolve().as_uri(),
            dest_valid_zip,
            progress_cb=lambda d, t: progress_calls.append((d, t)),
        )
        assert dest_valid_zip.exists(), "فایل معتبر دانلود نشد."
        assert len(progress_calls) > 0, "کال‌بک پیشرفت دانلود فراخوانی نشد."
        print("  ✓ تست download_update با کال‌بک پیشرفت دانلود با موفقیت گذشت.")

        # ۷. تست verify_stage
        extract_stage = sandbox / "data" / "test_stage"
        with zipfile.ZipFile(dest_valid_zip, "r") as zf:
            zf.extractall(extract_stage)

        valid, reason = verify_stage(extract_stage)
        assert valid is True, f"اعتبارسنجی استیج معتبر شکست خورد: {reason}"

        # تست اعتبارسنجی منفی (حذف main.py)
        (extract_stage / "main.py").unlink()
        valid_bad, _ = verify_stage(extract_stage)
        assert valid_bad is False, "اعتبارسنجی استیج با نبود main.py باید رد می‌شد."
        print("  ✓ تست verify_stage (اعتبارسنجی مثبت و منفی) با موفقیت گذشت.")

        # بازگرداندن فایل برای اعمال
        (extract_stage / "main.py").write_text("# NEW 14.0 MAIN CODE", encoding="utf-8")

        # ۸. تست apply_staged و بررسی لیست استثناها (حفظ اطلاعات مشتری)
        apply_res = apply_staged(sandbox, extract_stage)
        assert apply_res["version"] == "14.0", "نسخه اعمال شده اشتباه است."
        assert (sandbox / "main.py").read_text(encoding="utf-8") == "# NEW 14.0 MAIN CODE"
        assert json.loads((sandbox / "version.json").read_text(encoding="utf-8"))["version"] == "14.0"

        # بررسی قطعی عدم رونویسی فایل‌های مشتری
        assert (sandbox / "data" / "license.json").read_text(encoding="utf-8") == '{"token": "CUSTOMER_SECRET_LICENSE"}'
        assert (sandbox / "data" / "logs" / "playback.log").read_text(encoding="utf-8") == "CRITICAL_USER_LOGS"
        assert (sandbox / "data" / "last_port.txt").read_text(encoding="utf-8") == "9876"
        assert (sandbox / "admin_data" / "license_privkey.pem").read_text(encoding="utf-8") == "ADMIN_SECRET_KEY"
        assert not (sandbox / "admin_data" / "leaked.txt").exists(), "فایل ادمین نباید کپی می‌شد!"
        assert (sandbox / "runtime" / "dummy_browser.txt").read_text(encoding="utf-8") == "PORTABLE_BROWSER"
        print("  ✓ تست apply_staged و حفظ قطعی لایسنس و داده‌های مشتری (Exclusions) با موفقیت گذشت.")

        # ۹. تست شبیه‌سازی کامل حالت CLI (--full-update)
        # تنظیم مجدد sandbox به نسخه ۱۳.۰
        (sandbox / "version.json").write_text(json.dumps({"version": "13.0"}), encoding="utf-8")
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--full-update",
            "--port",
            "0",
            "--root",
            str(sandbox),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(sandbox))
        assert proc.returncode == 0, f"اجرای --full-update با خطا مواجه شد: {proc.stderr}\n{proc.stdout}"
        assert json.loads((sandbox / "version.json").read_text(encoding="utf-8"))["version"] == "14.0"
        assert (sandbox / "data" / "logs" / "updater.log").exists(), "فایل لاگ ساخته نشد."
        print("  ✓ تست اجرای خودکار CLI با پارامتر --full-update با موفقیت گذشت.")

        # تست اجرای مجدد زمانی که برنامه به روز است (باید با کد ۰ خارج شود)
        proc_re = subprocess.run(cmd, capture_output=True, text=True, cwd=str(sandbox))
        assert proc_re.returncode == 0, "خروج زمانی که برنامه به‌روز است با کد غیرصفر انجام شد."
        assert "به‌روز است" in (sandbox / "data" / "logs" / "updater.log").read_text(encoding="utf-8")
        print("  ✓ تست خروج تمیز کد ۰ در صورت به‌روز بودن سیستم با موفقیت گذشت.")

        # ۱۰. تست CLI حالت --apply-staged با پارامتر --port
        (sandbox / "version.json").write_text(json.dumps({"version": "13.0"}), encoding="utf-8")
        stage_dir_cli = sandbox / "data" / "update_stage"
        if stage_dir_cli.exists():
            shutil.rmtree(stage_dir_cli, ignore_errors=True)
        with zipfile.ZipFile(dest_valid_zip, "r") as zf:
            zf.extractall(stage_dir_cli)

        cmd_apply = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--apply-staged",
            "--port",
            "0",
            "--root",
            str(sandbox),
            "--stage-dir",
            str(stage_dir_cli),
        ]
        proc_apply = subprocess.run(cmd_apply, capture_output=True, text=True, cwd=str(sandbox))
        assert proc_apply.returncode == 0, f"اجرای --apply-staged با خطا مواجه شد: {proc_apply.stderr}\n{proc_apply.stdout}"
        assert json.loads((sandbox / "version.json").read_text(encoding="utf-8"))["version"] == "14.0"
        print("  ✓ تست اجرای خودکار CLI با پارامتر --apply-staged با موفقیت گذشت.")

        # ۱۱. تست Roundtrip از درخت واقعی فعلی پروژه (Current Tree Roundtrip)
        print("  ... اجرای تست Roundtrip با استفاده از درخت واقعی پروژه ...")
        real_tree_zip = remote_srv / "current_tree_update.zip"
        # ساخت زیپ از درخت فعلی بدون runtime، venv، git، admin_data
        with zipfile.ZipFile(real_tree_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for r_w, d_w, f_ws in os.walk(current_root):
                rel_r = Path(r_w).relative_to(current_root)
                # فیلتر کردن پوشه‌های مستثنی
                d_w[:] = [d for d in d_w if not is_excluded(rel_r / d if rel_r != Path(".") else Path(d))]
                for f_name in f_ws:
                    file_r = (rel_r / f_name) if rel_r != Path(".") else Path(f_name)
                    if is_excluded(file_r) or file_r == Path("version.json"):
                        continue
                    zf.write(Path(r_w) / f_name, file_r)
            # نوشتن version.json جدید با نسخه 13.1 در زیپ
            zf.writestr(
                "version.json",
                json.dumps({"version": "13.1", "notes": "بروزرسانی تستی درخت واقعی"}, ensure_ascii=False),
            )

        assert real_tree_zip.stat().st_size >= 1024 * 1024, "فایل زیپ درخت پروژه کمتر از ۱ مگابایت شد."

        # ساخت فایل version.json سرور فرضی
        tree_version_file = remote_srv / "tree_version.json"
        tree_version_file.write_text(
            json.dumps({
                "version": "13.1",
                "notes": "بروزرسانی تست راندتریپ",
                "zip_url": real_tree_zip.resolve().as_uri(),
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        # ساخت یک کپی موقت تمیز از پروژه به عنوان سیستم مشتری
        client_copy = tmp_dir / "client_copy"
        client_copy.mkdir()
        shutil.copy2(current_root / "main.py", client_copy / "main.py")
        shutil.copy2(current_root / "license.py", client_copy / "license.py")
        (client_copy / "static").mkdir(parents=True, exist_ok=True)
        shutil.copy2(current_root / "static" / "license.html", client_copy / "static" / "license.html")
        (client_copy / "launch.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        (client_copy / "version.json").write_text(json.dumps({"version": "13.0"}), encoding="utf-8")

        # ایجاد داده‌های خصوصی مشتری در client_copy
        (client_copy / "data").mkdir(parents=True, exist_ok=True)
        (client_copy / "data" / "license.json").write_text('{"token": "REAL_CUSTOMER_LICENSE_PRESERVED"}', encoding="utf-8")
        (client_copy / "data" / "logs").mkdir(parents=True, exist_ok=True)
        (client_copy / "data" / "logs" / "playback.log").write_text("EXISTING_CUSTOMER_LOGS", encoding="utf-8")
        (client_copy / "data" / "last_port.txt").write_text("18776", encoding="utf-8")

        # پیکربندی update_config
        cfg_client = load_config(client_copy)
        cfg_client["version_url"] = tree_version_file.resolve().as_uri()
        (client_copy / "update_config.json").write_text(json.dumps(cfg_client), encoding="utf-8")

        # اجرای بررسی، دانلود، استیج و اعمال
        chk_res = check_update(client_copy)
        assert chk_res["available"] is True
        assert chk_res["latest"] == "13.1"

        client_zip_dest = client_copy / "data" / "update_stage.zip"
        download_update(chk_res["zip_url"], client_zip_dest)
        assert client_zip_dest.stat().st_size >= 1024 * 1024

        client_stage_dir = client_copy / "data" / "update_stage"
        with zipfile.ZipFile(client_zip_dest, "r") as zf:
            zf.extractall(client_stage_dir)

        v_ok, v_why = verify_stage(client_stage_dir)
        assert v_ok is True, f"اعتبارسنجی استیج درخت پروژه ناموفق: {v_why}"

        applied = apply_staged(client_copy, client_stage_dir)
        assert applied["version"] == "13.1"
        assert applied["updated_files"] >= 3

        # بررسی مجدد پایداری داده‌های مشتری
        assert (client_copy / "data" / "license.json").read_text(encoding="utf-8") == '{"token": "REAL_CUSTOMER_LICENSE_PRESERVED"}'
        assert (client_copy / "data" / "logs" / "playback.log").read_text(encoding="utf-8") == "EXISTING_CUSTOMER_LOGS"
        assert (client_copy / "data" / "last_port.txt").read_text(encoding="utf-8") == "18776"
        assert json.loads((client_copy / "version.json").read_text(encoding="utf-8"))["version"] == "13.1"
        print("  ✓ تست Roundtrip کامل درخت واقعی پروژه با موفقیت گذشت.")

    print("\n[تست خودکار] تمامی آزمون‌های سامانه به‌روزرسانی با موفقیت کامل گذرانده شدند! ✔\n")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="موتور به‌روزرسانی امن و آفلاین SiteWorkflowAssistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--port", type=int, default=0, help="شماره پورت برای انتظار آزاد شدن پیش از اعمال تغییرات")
    parser.add_argument("--root", type=str, default=None, help="مسیر ریشه برنامه (پیش‌فرض: پوشه اسکریپت)")
    parser.add_argument("--stage-dir", type=str, default=None, help="مسیر پوشه استیج در حالت --apply-staged")
    parser.add_argument("--apply-staged", action="store_true", help="اعمال تغییرات استیج شده و راه‌اندازی مجدد")
    parser.add_argument("--full-update", action="store_true", help="اجرای فرآیند کامل بررسی، دانلود، استخراج، اعمال و اجرا")
    parser.add_argument("--check", action="store_true", help="بررسی نسخه و نمایش اطلاعات به‌روزرسانی")
    parser.add_argument("--self-test", action="store_true", help="اجرای آزمون‌های تشخیصی خودکار کامل")

    args = parser.parse_args()

    root = Path(args.root or Path(__file__).resolve().parent).resolve()

    try:
        if args.self_test:
            success = run_self_test(root)
            sys.exit(0 if success else 1)

        elif args.check:
            run_check_cli(root)
            sys.exit(0)

        elif args.apply_staged:
            stage_path = Path(args.stage_dir).resolve() if args.stage_dir else None
            run_apply_staged_cli(root, port=args.port, stage_dir=stage_path)
            sys.exit(0)

        elif args.full_update:
            run_full_update_cli(root, port=args.port)
            sys.exit(0)

        else:
            parser.print_help()
            sys.exit(0)

    except Exception as exc:
        log_msg(root, f"خطای بحرانی در فرآیند به‌روزرسانی: {exc}", level="ERROR")
        sys.exit(1)


if __name__ == "__main__":
    main()
