"""
تست زنده و انتها-به-انتهای (Live E2E) قابلیت‌های نسخه ۱۴ دستیار:
۱. تایمر شروع سرخطی (Scheduled Target-Time Buying & Rapid Retry)
۲. نشست پایدار مرورگر در صف خرید (Browser Reuse & Session Continuity)
۳. ریست نشست مرورگر (/api/easytrader/reset_session)
"""

import os
import subprocess
import sys
import time
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parent
PYTHON_EXE = str(ROOT / "runtime" / "python" / "python.exe")
BROWSERS_PATH = str(ROOT / "runtime" / "browsers")
PORT = 18776
BASE_URL = f"http://127.0.0.1:{PORT}"


def start_server():
    print(f"[*] راه‌اندازی سرور تستی روی پورت {PORT}...")
    env = dict(os.environ)
    env["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS_PATH
    env["PYTHONPATH"] = str(ROOT)
    
    proc = subprocess.Popen(
        [PYTHON_EXE, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT),
        env=env,
    )
    
    # انتظار تا بالا آمدن سرور و پاسخگویی اندپوینت لایسنس
    ready = False
    deadline = time.time() + 20.0
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=1.0) as client:
                r = client.get(f"{BASE_URL}/api/license/status")
                if r.status_code == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.4)
            
    if not ready:
        proc.kill()
        out, err = proc.communicate()
        raise RuntimeError(f"سرور روی پورت {PORT} بالا نیامد:\n{err.decode('utf-8', 'replace')}")
        
    print(f"[+] سرور با موفقیت روی پورت {PORT} مستقر و آماده پاسخگویی شد.")
    return proc


def run_test_1_scheduled_target_time(client: httpx.Client):
    print("\n" + "="*80)
    print("تست شماره ۱: تایمر شروع سرخطی و تکرار فوق‌سریع (Scheduled Target-Time & Rapid Retry)")
    print("="*80)
    
    # تعیین زمان هدف ۴ ثانیه بعد
    target_delay_s = 4.0
    now = time.time()
    target_epoch = now + target_delay_s
    target_str = time.strftime('%H:%M:%S', time.localtime(target_epoch))
    print(f"[*] زمان جاری سرور: {time.strftime('%H:%M:%S', time.localtime(now))}")
    print(f"[*] ثانیه هدف سرخطی: {target_str} (Target Epoch: {target_epoch:.2f} — {target_delay_s:.1f} ثانیه بعد)")
    
    scenario = {
        "version": 1,
        "start_url": f"{BASE_URL}/static/order-form/test_order_form.html",
        "turbo_mode": True,
        "reuse_browser": True,
        "keep_browser_open": True,
        "steps": [
            {
                "id": "goto_order_form",
                "type": "goto",
                "url": f"{BASE_URL}/static/order-form/test_order_form.html",
                "delay_ms": 0,
                "timeout_ms": 10000,
            },
            {
                "id": "submit_test",
                "type": "submit_until_confirmed",
                "selector": "button[data-cy=oms-order-form-submit-button-buy]",
                "qty_selector": "input[data-cy=order-form-input-quantity]",
                "qty_value": "250",
                "max_selector": "div[data-cy=order-form-max-price]",
                "target_epoch": target_epoch,
                "wait_s": 45.0,  # در حالت عادی ۴۵ ثانیه صبر دارد
                "max_attempts": 5,
                "delay_ms": 0,
                "timeout_ms": 15000,
            }
        ]
    }
    
    t_start = time.time()
    resp = client.post(
        f"{BASE_URL}/api/automation/playback",
        json={"scenario": scenario, "runs": [{"label": "تست زنده سرخطی", "values": {}}]},
        timeout=30.0,
    )
    t_end = time.time()
    total_elapsed = t_end - t_start
    
    print(f"[+] ارسال و اجرای سناریو به اتمام رسید (کل زمان: {total_elapsed:.2f} ثانیه).")
    res_data = resp.json()
    assert resp.status_code == 200, f"کد خطا دریافت شد {resp.status_code}: {res_data}"
    
    results = res_data.get("results", [])
    assert len(results) > 0, "نتیجه‌ای بازگردانده نشد!"
    run_res = results[0]
    assert run_res.get("ok") is True, f"اجرا با شکست مواجه شد: {run_res.get('error')}"
    
    logs = run_res.get("log") or run_res.get("logs", [])
    print("\n--- لاگ مراحل اجرای سرخطی و تکرار سریع ---")
    for line in logs:
        print("   ", line)
    print("-------------------------------------------\n")
    
    # راستی‌آزمایی دقیق
    # ۱. توقف و انتظار در فرم سفارش تا ثانیه هدف
    held_log = next((l for l in logs if "استقرار در فرم سفارش تکمیل شد. در انتظار ثانیه هدف" in l), None)
    assert held_log is not None, "لاگ انتظار تا ثانیه هدف یافت نشد!"
    print(f" [PASS] انتظار در فرم سفارش تا ثانیه هدف تأیید شد: '{held_log}'")
    
    # ۲. در تلاش اول پیام «خارج از ساعت معاملات» برگردانده شد و تکرار سریع ۰.۴ ثانیه‌ای فعال شد (به جای ۴۵ ثانیه)
    rapid_retry_log = next((l for l in logs if "ناموفق («خارج از ساعت»)؛ 0.4 ثانیه صبر" in l), None)
    assert rapid_retry_log is not None, "لاگ تکرار سریع ۰.۴ ثانیه‌ای یافت نشد!"
    print(f" [PASS] تکرار سریع (۰.۴ ثانیه به جای ۴۵ ثانیه پیش‌فرض) تأیید شد: '{rapid_retry_log}'")
    
    # ۳. تلاش دوم موفقیت‌آمیز بود و سفارش ثبت شد
    success_log = next((l for l in logs if "سفارش ثبت شد (تلاش 2)" in l), None)
    assert success_log is not None, "لاگ موفقیت تلاش دوم یافت نشد!"
    print(f" [PASS] ثبت سفارش در تلاش دوم تأیید شد: '{success_log}'")
    
    # ۴. زمان کل اجرا بررسی می‌شود: حدود ۴ ثانیه انتظار + ۱.۵ الی ۱.۸ ثانیه برای دو تلاش و ستل = حدود ۵.۵ الی ۶ ثانیه
    # در صورتی که وقفه ۴۵ ثانیه بود، زمان بالای ۴۹ ثانیه می‌شد.
    assert total_elapsed < 15.0, f"زمان اجرای سناریو ({total_elapsed:.2f}s) بیش از حد انتظار بود!"
    print(f" [PASS] راستی‌آزمایی زمان: کل زمان اجرا {total_elapsed:.2f} ثانیه بود (حلقه ۴۵ ثانیه‌ای اعمال نشد).")
    return True


def run_test_2_queue_continuity(client: httpx.Client):
    print("\n" + "="*80)
    print("تست شماره ۲: نشست پایدار مرورگر در صف خرید (Browser Reuse & Session Continuity)")
    print("="*80)
    
    # پاکسازی اولیه نشست
    r_reset_init = client.post(f"{BASE_URL}/api/easytrader/reset_session")
    print(f"[*] پاکسازی اولیه نشست: {r_reset_init.json()}")
    
    mock_base = f"{BASE_URL}/static/mock_easytrader.html"
    
    # سفارش ۱: نماد «فولاد» (شامل باز کردن مرورگر + لاگین + هدایت به پنل + خرید)
    print("\n--- اجرای سفارش اول در صف: نماد «فولاد» (باز کردن مرورگر و لاگین اولیه) ---")
    t1_start = time.time()
    payload_item1 = {
        "username": "user_test",
        "password": "pass_test",
        "stock": "فولاد",
        "qty": 100,
        "dry_run": False,
        "turbo_mode": True,
        "base_url": mock_base,
    }
    r1 = client.post(f"{BASE_URL}/api/easytrader/buy", json=payload_item1, timeout=120.0)
    t1_end = time.time()
    t1_duration = t1_end - t1_start
    print(f"[+] سفارش اول در {t1_duration:.2f} ثانیه انجام شد (HTTP {r1.status_code})")
    
    d1 = r1.json()
    assert r1.status_code == 200, f"سفارش اول شکست خورد: {d1}"
    res1 = d1.get("results", [])[0]
    assert res1.get("ok") is True, f"خطای سفارش اول: {res1.get('error')}"
    logs1 = res1.get("log", [])
    
    # بررسی انجام لاگین در سفارش اول
    assert any("fill #user-name" in l for l in logs1), "نام کاربری باید در سفارش اول تایپ شده باشد."
    assert any("fill #password" in l for l in logs1), "رمز عبور باید در سفارش اول تایپ شده باشد."
    assert any("سفارش ثبت شد" in l for l in logs1), "سفارش اول باید ثبت شده باشد."
    print(" [PASS] سفارش اول با موفقیت وارد سامانه شد و سفارش خرید فولاد را ثبت کرد (مرورگر باز نگه داشته شد).")
    
    # سفارش ۲: نماد «شستا» (استفاده مجدد از همان پنجره مرورگر بدون لاگین مجدد)
    print("\n--- اجرای سفارش دوم در صف: نماد «شستا» (استفاده مجدد از همان مرورگر) ---")
    t2_start = time.time()
    payload_item2 = {
        "username": "user_test",
        "password": "pass_test",
        "stock": "شستا",
        "qty": 200,
        "dry_run": False,
        "turbo_mode": True,
        "base_url": mock_base,
    }
    r2 = client.post(f"{BASE_URL}/api/easytrader/buy", json=payload_item2, timeout=120.0)
    t2_end = time.time()
    t2_duration = t2_end - t2_start
    print(f"[+] سفارش دوم در {t2_duration:.2f} ثانیه انجام شد (HTTP {r2.status_code})")
    
    d2 = r2.json()
    assert r2.status_code == 200, f"سفارش دوم شکست خورد: {d2}"
    res2 = d2.get("results", [])[0]
    assert res2.get("ok") is True, f"خطای سفارش دوم: {res2.get('error')}"
    logs2 = res2.get("log", [])
    
    print("\n--- لاگ مراحل سفارش دوم (سفارش بعدی صف در همان مرورگر) ---")
    for l in logs2:
        print("   ", l)
    print("----------------------------------------------------------\n")
    
    # راستی‌آزمایی تداوم نشست در سفارش دوم:
    # ۱. عدم بازکردن مجدد آدرس صفحه لاگین (رد شدن goto_easytrader با شرط آدرس)
    skip_goto = next((l for l in logs2 if "قدم goto_easytrader رد شد (شرط آدرس)" in l), None)
    assert skip_goto is not None, "قدم goto_easytrader باید رد می‌شد!"
    print(f" [PASS] رد شدن بازکردن مجدد صفحه ورود: '{skip_goto}'")
    
    # ۲. رد شدن هر ۴ مرحله لاگین بدون معطلی (۰ میلی‌ثانیه): fill_username, fill_password, clk_login_submit, verify_login
    skip_user = next((l for l in logs2 if "قدم fill_username رد شد (شرط آدرس)" in l), None)
    skip_pass = next((l for l in logs2 if "قدم fill_password رد شد (شرط آدرس)" in l), None)
    skip_submit = next((l for l in logs2 if "قدم clk_login_submit رد شد (شرط آدرس)" in l), None)
    skip_verify = next((l for l in logs2 if "قدم verify_login رد شد (شرط آدرس)" in l), None)
    
    assert skip_user is not None, "قدم fill_username باید رد می‌شد!"
    assert skip_pass is not None, "قدم fill_password باید رد می‌شد!"
    assert skip_submit is not None, "قدم clk_login_submit باید رد می‌شد!"
    assert skip_verify is not None, "قدم verify_login باید رد می‌شد!"
    print(f" [PASS] تمام مراحل لاگین در سفارش دوم رد شدند (۰ میلی‌ثانیه معطلی):")
    print(f"        1. {skip_user}")
    print(f"        2. {skip_pass}")
    print(f"        3. {skip_submit}")
    print(f"        4. {skip_verify}")
    
    # ۳. ورود مستقیم به سرچ و خرید نماد دوم (شستا)
    assert any("شستا" in l for l in logs2), "سفارش دوم باید با نماد شستا کار می‌کرد."
    assert any("سفارش ثبت شد" in l for l in logs2), "سفارش دوم باید ثبت سفارش را تأیید می‌کرد."
    print(" [PASS] ورود مستقیم به جستجو و ثبت قطعی خرید نماد دوم (شستا) در همان نشست تأیید شد.")
    
    # تست اندپوینت ریست نشست: /api/easytrader/reset_session
    print("\n--- تست اندپوینت بستن و ریست نشست مرورگر (/api/easytrader/reset_session) ---")
    r_reset1 = client.post(f"{BASE_URL}/api/easytrader/reset_session")
    d_res1 = r_reset1.json()
    print(f"[+] فراخوانی ۱: {d_res1}")
    assert d_res1.get("ok") is True and d_res1.get("closed") is True, f"انتظار closed=True، دریافت شد: {d_res1}"
    print(" [PASS] مرورگر با موفقیت بسته شد (closed=True).")
    
    # فراخوانی مجدد برای اطمینان از پاکسازی
    r_reset2 = client.post(f"{BASE_URL}/api/easytrader/reset_session")
    d_res2 = r_reset2.json()
    print(f"[+] فراخوانی ۲: {d_res2}")
    assert d_res2.get("ok") is True and d_res2.get("closed") is False, f"انتظار closed=False، دریافت شد: {d_res2}"
    print(" [PASS] فراخوانی مجدد تایید کرد که هیچ مرورگر بازی باقی نمانده (closed=False).")
    
    return True


def main():
    proc = None
    try:
        proc = start_server()
        with httpx.Client(timeout=120.0) as client:
            t1_ok = run_test_1_scheduled_target_time(client)
            t2_ok = run_test_2_queue_continuity(client)
            
        print("\n" + "="*80)
        print("تمامی تست‌های زنده (Live E2E) با موفقیت ۱۰۰٪ پاس شدند!")
        print("="*80)
    finally:
        if proc:
            print("\n[*] در حال خاتمه دادن به پردازه سرور تستی...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            print("[+] پردازه سرور خاموش شد و پورت ۱۸۷۷۶ آزاد گردید.")


if __name__ == "__main__":
    main()
