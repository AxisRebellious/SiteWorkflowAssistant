"""لانچر پورت‌شناور: اولین پورت آزاد را از بین نامزدها برمی‌دارد، سرور را بالا می‌آورد و صفحه محصول را باز می‌کند.

چرا؟ رنج پورت‌های محروم ویندوز (Hyper-V/winnat) روی هر سیستم فرق می‌کند و ممکن است
پورت ثابت (مثل 9876) روی سیستم مشتری بسته باشد. این لانچر روی هر سیستمی خودش راه را پیدا می‌کند.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CANDIDATE_PORTS = [9876, 18776, 28776, 47876, 58776]


def pick_port() -> int:
    cands: list[int] = []
    env_port = (os.environ.get("SWA_PORT") or "").strip()
    if env_port.isdigit():
        cands.append(int(env_port))
    cands.extend(CANDIDATE_PORTS)
    for port in cands:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", port))
            sock.close()
            return port
        except OSError:
            continue
    return CANDIDATE_PORTS[0]


def main() -> None:
    env = dict(os.environ)
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / "runtime" / "browsers")
    port = pick_port()
    try:
        (ROOT / "data").mkdir(exist_ok=True)
        (ROOT / "data" / "last_port.txt").write_text(str(port), encoding="utf-8")
    except Exception:
        pass
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
    )
    url = f"http://127.0.0.1:{port}/"
    try:
        import urllib.request

        for _ in range(60):
            try:
                urllib.request.urlopen(url + "api/license/status", timeout=3)
                break
            except Exception:
                time.sleep(0.5)
    except Exception:
        pass
    try:
        webbrowser.open_new_tab(f"{url}?t={int(time.time())}")
    except Exception:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print(f"READY {url}", flush=True)
    proc.wait()


if __name__ == "__main__":
    main()
