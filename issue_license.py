"""ابزار صدور و فعال‌سازی لایسنس (ویژه مالک / ادمین).

استفاده:
    runtime/python/python.exe issue_license.py <نام_کاربری> <تعداد_روز> [--activate]

مثال صدور برای مشتری:
    runtime/python/python.exe issue_license.py user123 30

مثال فعال‌سازی مستقیم روی همین سیستم برای مالک:
    runtime/python/python.exe issue_license.py myuser 365 --activate
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import license as lic


def main() -> None:
    parser = argparse.ArgumentParser(description="صدور و فعال‌سازی توکن لایسنس")
    parser.add_argument("username", help="نام کاربری یا کدملی مشتری / کاربر")
    parser.add_argument("days", type=int, default=30, nargs="?", help="تعداد روزهای اعتبار (پیش‌فرض ۳۰)")
    parser.add_argument(
        "--activate",
        action="store_true",
        help="ذخیره و فعال‌سازی مستقیم در data/license.json روی همین سیستم",
    )
    args = parser.parse_args()

    privkey_path = ROOT / "admin_data" / "license_privkey.pem"
    if not privkey_path.is_file():
        print(f"خطا: کلید خصوصی ادمین در {privkey_path} یافت نشد.")
        sys.exit(1)

    privkey = lic.load_privkey(str(privkey_path))
    token, exp_date = lic.issue_token(args.username, args.days, privkey=privkey)

    print("=" * 60)
    print("✅ توکن لایسنس با موفقیت صادر شد:")
    print(f"  کاربر: {args.username}")
    print(f"  اعتبار: {args.days} روز")
    print(f"  تاریخ انقضا: {exp_date}")
    print(f"  کد توکن:")
    print(token)
    print("=" * 60)

    if args.activate:
        data_dir = ROOT / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        lic_file = data_dir / "license.json"
        lic_payload = {
            "username": args.username.strip(),
            "token": token,
            "activated_at": datetime.now(lic.TEHRAN).isoformat(),
        }
        with open(lic_file, "w", encoding="utf-8") as f:
            json.dump(lic_payload, f, ensure_ascii=False, indent=2)
        print("🎉 لایسنس مستقیماً در data/license.json ثبت و فعال شد.")


if __name__ == "__main__":
    main()
