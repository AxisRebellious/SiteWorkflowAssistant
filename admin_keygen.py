"""ساخت کلید لایسنس ادمین — فقط یک‌بار روی سیستم ادمین اجرا شود.

    runtime/python/python.exe admin_keygen.py

- کلید خصوصی → admin_data/license_privkey.pem (محرمانه؛ هرگز داخل زیپ مشتری)
- کلید عمومی → داخل license.py (PUBLIC_KEY_PEM) جایگذاری می‌شود
- این فایل و پوشه admin_data هرگز داخل زیپ مشتری نمی‌روند.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    import license as lic

    if lic.PUBLIC_KEY_PEM:
        print("PUBLIC_KEY_PEM از قبل تنظیم شده؛ برای ساخت مجدد اول آن را خالی کنید.")
        return

    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    admin_dir = ROOT / "admin_data"
    admin_dir.mkdir(exist_ok=True)
    (admin_dir / "license_privkey.pem").write_bytes(priv_pem)

    src = (ROOT / "license.py").read_text(encoding="utf-8")
    old = 'PUBLIC_KEY_PEM = b""'
    assert old in src, "الگوی جایگذاری در license.py پیدا نشد."
    new = "PUBLIC_KEY_PEM = (\n" + "\n".join(
        f'    b"{line}"' for line in pub_pem.decode("ascii").strip().splitlines()
    ) + "\n)"
    (ROOT / "license.py").write_text(src.replace(old, new), encoding="utf-8")

    print("OK: کلید ساخته شد.")
    print("  private: admin_data/license_privkey.pem  (محرمانه!)")
    print("  public : داخل license.py ثبت شد.")


if __name__ == "__main__":
    main()
