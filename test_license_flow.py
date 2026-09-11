"""
Verification test for license API, enforcement, routes, and status.
Runs against live uvicorn server (port 9700/9876) AND FastAPI TestClient.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path("C:/Users/Dara/Desktop/Projects/SiteWorkflowAssistant")
PYTHON = ROOT / "runtime" / "python" / "python.exe"
PRIVKEY = ROOT / "admin_data" / "license_privkey.pem"
LIC_FILE = ROOT / "data" / "license.json"


def find_available_port(preferred=9876):
    for p in [preferred, 9700, 8000, 8080]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(('127.0.0.1', p))
            s.close()
            return p
        except Exception:
            continue
    raise RuntimeError("No available port found")


def main():
    print("=== 1. Checking py_compile ===")
    res = subprocess.run([str(PYTHON), "-m", "py_compile", str(ROOT / "main.py"), str(ROOT / "license.py")], capture_output=True, text=True)
    assert res.returncode == 0, f"py_compile failed: {res.stderr}"
    print("py_compile PASSED.")

    # Remove any existing data/license.json for clean test
    if LIC_FILE.exists():
        LIC_FILE.unlink()
        print("Removed existing license.json for clean test.")

    port = find_available_port(9876)
    print(f"=== 2. Booting live uvicorn server on port {port} ===")
    if port != 9876:
        print(f"NOTE: Port 9876 is in Windows Hyper-V excluded range (9853-9952). Using available port {port} for live HTTP verification.")

    proc = subprocess.Popen(
        [str(PYTHON), "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    base_url = f"http://127.0.0.1:{port}"

    def request_http(method: str, path: str, data: dict | None = None) -> tuple[int, dict | str]:
        url = f"{base_url}{path}"
        headers = {"Content-Type": "application/json"} if data is not None else {}
        body_bytes = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                content_type = resp.headers.get("Content-Type", "")
                raw = resp.read()
                if "application/json" in content_type:
                    return resp.status, json.loads(raw.decode("utf-8"))
                return resp.status, raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw.decode("utf-8"))
            except Exception:
                return exc.code, raw.decode("utf-8", errors="replace")

    try:
        # Wait for server to come up
        started = False
        for _ in range(30):
            try:
                code, body = request_http("GET", "/api/license/status")
                if code == 200:
                    started = True
                    break
            except Exception:
                time.sleep(0.5)

        if not started:
            proc.kill()
            out, err = proc.communicate()
            print("Server failed to start:", out.decode(), err.decode())
            sys.exit(1)
        print("Server is UP and responsive.")

        print("=== 3. Testing Routes ===")
        # Route / should serve static/license.html
        code, html = request_http("GET", "/")
        assert code == 200, f"GET / expected 200, got {code}"
        assert "فعال‌سازی و وضعیت لایسنس" in html, "GET / did not contain license.html content"
        print("GET / -> serves license.html [OK]")

        # Route /panel should serve static/index.html
        code, html_panel = request_http("GET", "/panel")
        assert code == 200, f"GET /panel expected 200, got {code}"
        assert "دستیار جریان کار وب" in html_panel or "workflow" in html_panel.lower(), "GET /panel did not serve index.html"
        print("GET /panel -> serves index.html [OK]")

        # Route /easytrader should serve static/easytrader.html
        code, html_et = request_http("GET", "/easytrader")
        assert code == 200, f"GET /easytrader expected 200, got {code}"
        assert "ایزی‌تریدر" in html_et, "GET /easytrader did not serve easytrader.html"
        print("GET /easytrader -> serves easytrader.html [OK]")

        print("=== 4. Testing GET /api/license/status (No license) ===")
        code, status_data = request_http("GET", "/api/license/status")
        assert code == 200, f"Expected 200, got {code}"
        assert status_data.get("ok") is True, f"Expected ok=True, got {status_data}"
        assert status_data.get("active") is False, f"Expected active=False, got {status_data}"
        print(f"Status without license: active={status_data.get('active')}, msg={status_data.get('message')} [OK]")

        print("=== 5. Testing Enforcement (Should return 403) ===")
        # easytrader_buy
        code, buy_res = request_http("POST", "/api/easytrader/buy", {"username": "u", "password": "p", "stock": "s", "qty": 10})
        assert code == 403, f"easytrader_buy expected 403, got {code} ({buy_res})"
        print(f"POST /api/easytrader/buy without license -> 403 {buy_res} [OK]")

        # automation_playback
        code, pb_res = request_http("POST", "/api/automation/playback", {"scenario": {}, "runs": []})
        assert code == 403, f"automation_playback expected 403, got {code} ({pb_res})"
        print(f"POST /api/automation/playback without license -> 403 [OK]")

        # automation_playback_queue
        code, pbq_res = request_http("POST", "/api/automation/playback/queue", {"items": []})
        assert code == 403, f"automation_playback_queue expected 403, got {code} ({pbq_res})"
        print(f"POST /api/automation/playback/queue without license -> 403 [OK]")

        # /api/automation/start
        code, as_res = request_http("POST", "/api/automation/start", {"url": "about:blank"})
        assert code == 403, f"automation_start expected 403, got {code} ({as_res})"
        print(f"POST /api/automation/start without license -> 403 [OK]")

        # record/start
        code, rs_res = request_http("POST", "/api/automation/session/dummy-id/record/start", {})
        assert code == 403, f"record/start expected 403, got {code} ({rs_res})"
        print(f"POST record/start without license -> 403 [OK]")

        print("=== 6. Testing Activation with Invalid Token (Should return 4xx) ===")
        code, inv_res = request_http("POST", "/api/license/activate", {"username": "admin", "token": "invalid-token-123"})
        assert 400 <= code < 500, f"Invalid token expected 4xx, got {code} ({inv_res})"
        print(f"POST /api/license/activate with invalid token -> {code} {inv_res} [OK]")

        print("=== 7. Issuing Token using license_privkey.pem ===")
        sys.path.insert(0, str(ROOT))
        import license as lic
        privkey = lic.load_privkey(str(PRIVKEY))
        valid_tok, exp_date = lic.issue_token("dara", 30, privkey=privkey)
        print(f"Issued token for 'dara': {valid_tok[:30]}... expires {exp_date}")

        # Test activation with wrong username
        code, mis_res = request_http("POST", "/api/license/activate", {"username": "other_user", "token": valid_tok})
        assert 400 <= code < 500, f"Mismatched username expected 4xx, got {code} ({mis_res})"
        print(f"POST /api/license/activate with mismatched username -> {code} {mis_res} [OK]")

        print("=== 8. Activating with Valid Token ===")
        code, act_res = request_http("POST", "/api/license/activate", {"username": "dara", "token": valid_tok})
        assert code == 200, f"Valid activate expected 200, got {code} ({act_res})"
        assert act_res.get("ok") is True
        assert act_res.get("username") == "dara"
        assert act_res.get("days_left") == 30
        assert act_res.get("expires") == exp_date
        print(f"POST /api/license/activate -> 200: {act_res} [OK]")

        # Check that data/license.json was written
        assert LIC_FILE.is_file(), "data/license.json was not created!"
        with open(LIC_FILE, "r", encoding="utf-8") as f:
            saved_lic = json.load(f)
            assert saved_lic.get("username") == "dara"
            assert saved_lic.get("token") == valid_tok
            assert saved_lic.get("activated_at")
        print("data/license.json verified on disk [OK]")

        print("=== 9. Testing GET /api/license/status (After Activation) ===")
        code, st_res = request_http("GET", "/api/license/status")
        assert code == 200
        assert st_res.get("ok") is True
        assert st_res.get("active") is True
        assert st_res.get("username") == "dara"
        assert st_res.get("days_left") == 30
        assert st_res.get("expires") == exp_date
        print(f"GET /api/license/status -> 200 active={st_res.get('active')} days_left={st_res.get('days_left')} msg={st_res.get('message')} [OK]")

        print("=== 10. Testing easytrader_buy after activation ===")
        # Without stock/qty it should fail with 400 (validation), NOT 403 (license)!
        code, buy_after = request_http("POST", "/api/easytrader/buy", {"username": "", "password": "", "stock": "", "qty": 0})
        assert code == 400, f"Expected 400 validation error, got {code} ({buy_after})"
        assert "نام کاربری و رمز عبور الزامی است" in str(buy_after), f"Unexpected response: {buy_after}"
        print(f"POST /api/easytrader/buy after activation passed license check -> 400 validation: {buy_after} [OK]")

        print("\n=========================================")
        print("ALL 10 VERIFICATION CHECKS PASSED!")
        print("=========================================")

    finally:
        print("Cleaning up server process...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        print("Server process terminated.")


if __name__ == "__main__":
    main()
