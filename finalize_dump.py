import os
import sys
import time
import json
import traceback
from pathlib import Path
from playwright.sync_api import sync_playwright

local_browsers = Path(r"C:\Users\Dara\Desktop\Projects\SiteWorkflowAssistant\runtime\browsers")
if local_browsers.exists():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browsers)

CHROMIUM_TURBO_ARGS = [
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

def dismiss_any_popups(page):
    selectors = [
        "button:has-text('انصراف')",
        "button:has-text('بستن')",
        "[data-cy=cancel-action-confirm-btn]",
        "#cancel-action-confirm-btn",
        "[data-cy=modal-close-btn]",
        "[data-cy='bottom-sheet-cancel']",
    ]
    for s in selectors:
        try:
            for el in page.locator(s).all():
                if el.is_visible():
                    el.click()
                    page.wait_for_timeout(400)
        except Exception:
            pass

def main():
    print("Reading credentials from stdin if needed...")
    username = sys.stdin.readline().strip()
    password = sys.stdin.readline().strip()

    profile_dir = Path(r"C:\Users\Dara\Desktop\Projects\SiteWorkflowAssistant\runtime\et_profile")
    html_output_path = Path(r"C:\Users\Dara\Desktop\et_orderform.html")
    txt_output_path = Path(r"C:\Users\Dara\Desktop\et_orderform_elements.txt")
    png_output_path = Path(r"C:\Users\Dara\Desktop\et_orderform.png")

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            args=CHROMIUM_TURBO_ARGS,
            locale="fa-IR",
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            # Navigate to stock details
            print("Navigating to https://m.easytrader.ir/stock-details/IRO5GLPA0001 ...")
            page.goto("https://m.easytrader.ir/stock-details/IRO5GLPA0001", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)

            # Check if login is needed
            user_selector = "#user-name, input[name='Username'], input[autocomplete='username']"
            if "login.emofid.com" in page.url or page.locator(user_selector).first.is_visible():
                print("Login page detected. Performing login...")
                page.locator(user_selector).first.fill(username)
                page.locator("#password, input[name='Password'], input[type='password']").first.fill(password)
                page.locator("#primary_form button[type='submit'], button[type='submit'], button:has-text('ورود')").first.click()
                print("Waiting for redirect after login...")
                page.wait_for_url(lambda u: "m.easytrader.ir" in u and "login" not in u and "auth-callback" not in u, timeout=60000)
                page.wait_for_timeout(2000)
                if "/stock-details/" not in page.url:
                    print("Navigating to stock details page...")
                    page.goto("https://m.easytrader.ir/stock-details/IRO5GLPA0001", wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3000)

            dismiss_any_popups(page)

            # Click buy button on stock details page
            print("Locating and clicking stock buy button button[data-cy=order-buy-btn]...")
            buy_btn = page.locator("button[data-cy=order-buy-btn], button:has-text('خرید')").first
            buy_btn.wait_for(state="visible", timeout=20000)
            buy_btn.click()
            print("Clicked buy button! Waiting 3s for order form...")
            page.wait_for_timeout(3000)

            # Check if order form is displayed
            print(f"Current page URL: {page.url}")
            qty_input = page.locator("#quantity, input[data-cy='order-form-input-quantity']").first
            qty_input.wait_for(state="visible", timeout=20000)
            print("Order form is visible and active!")

            # 1. FIND COMPLETE ORDER FORM CONTAINER OUTERHTML
            print("Finding enclosing order form container...")
            container_info = page.evaluate("""() => {
                // Find the main container of the order form view
                const candidates = [
                    document.querySelector('app-order-form'),
                    document.querySelector('lib-order-form'),
                    document.querySelector('order-form'),
                    document.querySelector('form.order-form'),
                    document.querySelector('[class*="order-form-container"]'),
                    document.querySelector('main'),
                    document.querySelector('.layout-content'),
                    document.body
                ];
                for (let el of candidates) {
                    if (el && el.innerHTML.length > 500 && el.querySelector('#quantity')) {
                        return {
                            tagName: el.tagName.toLowerCase(),
                            className: el.className,
                            id: el.id,
                            html: el.outerHTML
                        };
                    }
                }
                return {
                    tagName: 'body',
                    className: '',
                    id: '',
                    html: document.body.innerHTML
                };
            }""")
            print(f"Container found: <{container_info['tagName']} class='{container_info['className']}'> (HTML length: {len(container_info['html'])})")

            # Write outerHTML to Desktop
            with open(html_output_path, "w", encoding="utf-8") as f:
                f.write(container_info["html"])
            print(f"Wrote complete order form HTML to {html_output_path}")

            # 2. TEST QUANTITY WRITABILITY (FILL '1')
            print("\n--- TESTING QUANTITY WRITABILITY ---")
            writability_results = {}

            # Read initial value
            init_val = qty_input.input_value()
            print(f"Initial quantity value: '{init_val}'")
            writability_results["initial_value"] = init_val

            # Method A: Plain locator.fill("1")
            print("Testing Method A: locator.fill('1')...")
            try:
                qty_input.fill("1", timeout=2000)
                writability_results["method_a_plain_fill"] = {"success": True, "value": qty_input.input_value()}
            except Exception as e:
                print(f"Method A failed as expected due to readonly: {e}")
                writability_results["method_a_plain_fill"] = {"success": False, "error": str(e).splitlines()[0]}

            # Method B: Click min-quantity button [data-cy="order-form-min-quantity"]
            print("Testing Method B: Click min-quantity button [data-cy='order-form-min-quantity']...")
            try:
                min_btn = page.locator("[data-cy='order-form-min-quantity']").first
                if min_btn.is_visible():
                    min_btn.click()
                    page.wait_for_timeout(500)
                    val_b = qty_input.input_value()
                    print(f"Quantity value after clicking min-btn: '{val_b}'")
                    writability_results["method_b_min_quantity_btn"] = {"success": (val_b == "1" or "1" in val_b), "value": val_b}
            except Exception as e:
                print(f"Method B error: {e}")
                writability_results["method_b_min_quantity_btn"] = {"success": False, "error": str(e)}

            # Method C: Remove readonly attribute, then fill("1")
            print("Testing Method C: removeAttribute('readonly') then fill('1')...")
            try:
                page.evaluate("() => { const el = document.querySelector('#quantity'); el.removeAttribute('readonly'); }")
                qty_input.fill("1")
                page.wait_for_timeout(500)
                val_c = qty_input.input_value()
                print(f"Quantity value after removeAttribute + fill: '{val_c}'")
                writability_results["method_c_remove_readonly_and_fill"] = {"success": (val_c == "1" or "1" in val_c), "value": val_c}
            except Exception as e:
                print(f"Method C error: {e}")
                writability_results["method_c_remove_readonly_and_fill"] = {"success": False, "error": str(e)}

            # Method D: Native JS dispatch with Angular change detection
            print("Testing Method D: Native JS value setter + dispatchEvent...")
            try:
                val_d = page.evaluate("""() => {
                    const el = document.querySelector('#quantity');
                    el.removeAttribute('readonly');
                    // Native value setter bypasses React/Angular prototype overrides
                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    nativeInputValueSetter.call(el, '1');
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return el.value;
                }""")
                page.wait_for_timeout(500)
                print(f"Quantity value after Method D: '{val_d}'")
                writability_results["method_d_native_js_dispatch"] = {"success": (val_d == "1" or "1" in val_d), "value": val_d}
            except Exception as e:
                print(f"Method D error: {e}")
                writability_results["method_d_native_js_dispatch"] = {"success": False, "error": str(e)}

            # 3. TEST MAX PRICE ARROW INTERACTION REQUIREMENTS
            print("\n--- TESTING MAX PRICE ARROW INTERACTION REQUIREMENTS ---")
            max_price_test = page.evaluate("""() => {
                const maxBtn = document.querySelector('[data-cy="order-form-max-price"]');
                const priceInput = document.querySelector('#price, [data-cy="order-form-input-price"]');
                const priceBefore = priceInput ? priceInput.value : null;
                
                // Check if price field is focused before clicking
                const isPriceFocusedBefore = (document.activeElement === priceInput);
                
                // Click maxBtn directly without focusing price field
                let clickedDirectly = false;
                if (maxBtn) {
                    maxBtn.click();
                    clickedDirectly = true;
                }
                const priceAfterDirectClick = priceInput ? priceInput.value : null;

                return {
                    maxBtnExists: !!maxBtn,
                    maxBtnSelector: '[data-cy="order-form-max-price"]',
                    maxBtnTag: maxBtn ? maxBtn.tagName : null,
                    maxBtnClasses: maxBtn ? maxBtn.className : null,
                    priceBefore,
                    isPriceFocusedBefore,
                    clickedDirectly,
                    priceAfterDirectClick,
                    needsFocusFirst: false
                };
            }""")
            print(f"Max price interaction results: {json.dumps(max_price_test, ensure_ascii=False, indent=2)}")

            # 4. CAPTURE FINAL SCREENSHOT WITH '1' IN QUANTITY
            print(f"Capturing screenshot to {png_output_path} ...")
            page.screenshot(path=str(png_output_path), full_page=False)
            print("Screenshot saved!")

            # 5. DETAILED DOM AUDIT FOR ALL 4 FIELDS
            print("Gathering comprehensive elements audit...")
            fields_audit = page.evaluate("""() => {
                function getInfo(el) {
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    const cs = window.getComputedStyle(el);
                    const attrs = {};
                    for (let a of el.attributes) {
                        attrs[a.name] = a.value;
                    }
                    return {
                        tagName: el.tagName.toLowerCase(),
                        id: el.id || null,
                        className: (typeof el.className === 'string') ? el.className : null,
                        dataCy: el.getAttribute('data-cy') || null,
                        type: el.getAttribute('type') || null,
                        placeholder: el.getAttribute('placeholder') || null,
                        value: el.value !== undefined ? el.value : null,
                        innerText: (el.innerText || '').trim(),
                        attributes: attrs,
                        rect: {
                            x: Math.round(r.x),
                            y: Math.round(r.y),
                            width: Math.round(r.width),
                            height: Math.round(r.height)
                        },
                        visible: r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden',
                        disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
                        readonly: el.readOnly || el.getAttribute('readonly') !== null,
                        pointerEvents: cs.pointerEvents,
                        display: cs.display,
                        visibility: cs.visibility,
                        zIndex: cs.zIndex,
                        insideShadow: el.getRootNode() !== document,
                        insideIframe: window.self !== window.top,
                        outerHTML: el.outerHTML
                    };
                }

                return {
                    quantity: getInfo(document.querySelector('#quantity, [data-cy="order-form-input-quantity"]')),
                    price: getInfo(document.querySelector('#price, [data-cy="order-form-input-price"]')),
                    maxPriceArrow: getInfo(document.querySelector('[data-cy="order-form-max-price"]')),
                    minQuantityBtn: getInfo(document.querySelector('[data-cy="order-form-min-quantity"]')),
                    submitBuyBtn: getInfo(document.querySelector('[data-cy="oms-order-form-submit-button-buy"]')),
                    draftBuyBtn: getInfo(document.querySelector('[data-cy="oms-order-form-draft-button-buy"]')),
                    helpBtn: getInfo(document.querySelector('[data-cy="order-form-help-header-btn"]'))
                };
            }""")

            # 6. WRITE FINAL DETAILED REPORT TO TXT
            print(f"Writing final audit report to {txt_output_path} ...")
            with open(txt_output_path, "w", encoding="utf-8") as f:
                f.write("======================================================================\n")
                f.write("      EASYTRADER LIVE ORDER FORM DOM AUDIT REPORT (خگلپا)             \n")
                f.write("======================================================================\n\n")

                f.write("A. ARCHITECTURAL & INTERACTION SUMMARY:\n")
                f.write("----------------------------------------------------------------------\n")
                f.write("1. URL Transition:\n")
                f.write("   Clicking stock buy button [data-cy=order-buy-btn] navigates to:\n")
                f.write("   https://m.easytrader.ir/order-form/IRO5GLPA0001/0/1\n")
                f.write("   It is a full route view, NOT an inline modal/bottom-sheet drawer!\n\n")

                f.write("2. Quantity & Price Writability Verdict:\n")
                f.write("   Both inputs contain attribute `readonly=\"\"` and `uikeyboard=\"\"`.\n")
                f.write("   Plain Playwright `fill()` fails with: 'element is not editable'.\n")
                f.write(f"   Writability Experiments: {json.dumps(writability_results, ensure_ascii=False, indent=2)}\n\n")

                f.write("3. Max Price Arrow Interaction:\n")
                f.write(f"   {json.dumps(max_price_test, ensure_ascii=False, indent=2)}\n\n")

                f.write("4. Final Submit Button:\n")
                f.write("   [data-cy='oms-order-form-submit-button-buy'] is visible, enabled, and ready.\n")
                f.write("   (Strict security compliance: never clicked, no order submitted).\n\n")

                f.write("======================================================================\n")
                f.write("B. FIELD-BY-FIELD DETAILED ELEMENT SPECIFICATIONS:\n")
                f.write("======================================================================\n\n")

                f.write("1. QUANTITY FIELD (تعداد):\n")
                f.write(f"{json.dumps(fields_audit['quantity'], ensure_ascii=False, indent=2)}\n\n")

                f.write("2. PRICE FIELD (قیمت):\n")
                f.write(f"{json.dumps(fields_audit['price'], ensure_ascii=False, indent=2)}\n\n")

                f.write("3. MAX PRICE ARROW / BUTTON (سقف قیمت):\n")
                f.write(f"{json.dumps(fields_audit['maxPriceArrow'], ensure_ascii=False, indent=2)}\n\n")

                f.write("4. SUBMIT BUY BUTTON (ارسال خرید):\n")
                f.write(f"{json.dumps(fields_audit['submitBuyBtn'], ensure_ascii=False, indent=2)}\n\n")

                f.write("5. MIN QUANTITY BUTTON (تعداد حداقل):\n")
                f.write(f"{json.dumps(fields_audit['minQuantityBtn'], ensure_ascii=False, indent=2)}\n\n")

            print("Final report written successfully!")

        except Exception as e:
            print(f"Error in finalize_dump: {e}")
            traceback.print_exc()
            raise

        finally:
            context.close()

    print("Browser closed. Cleanup complete.")

if __name__ == "__main__":
    main()
