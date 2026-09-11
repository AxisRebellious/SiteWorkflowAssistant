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
    dismissed = []
    for s in selectors:
        try:
            elements = page.locator(s).all()
            for el in elements:
                if el.is_visible():
                    txt = el.inner_text().strip()
                    print(f"Dismissing popup/bottom-sheet: selector='{s}', text='{txt}'")
                    el.click()
                    dismissed.append(f"{s} ({txt})")
                    page.wait_for_timeout(800)
        except Exception:
            pass
    return dismissed

def main():
    print("[1/10] Reading credentials securely from stdin...")
    username = sys.stdin.readline().strip()
    password = sys.stdin.readline().strip()

    if not username or not password:
        print("ERROR: Empty credentials provided.")
        sys.exit(1)

    print("[2/10] Starting Playwright with persistent context...")
    profile_dir = Path(r"C:\Users\Dara\Desktop\Projects\SiteWorkflowAssistant\runtime\et_profile")
    profile_dir.mkdir(parents=True, exist_ok=True)

    html_output_path = Path(r"C:\Users\Dara\Desktop\et_orderform.html")
    txt_output_path = Path(r"C:\Users\Dara\Desktop\et_orderform_elements.txt")
    png_output_path = Path(r"C:\Users\Dara\Desktop\et_orderform.png")
    err_output_path = Path(r"C:\Users\Dara\Desktop\et_fatal_error.png")

    search_experiments = {}

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            args=CHROMIUM_TURBO_ARGS,
            locale="fa-IR",
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else context.new_page()

        # Track network requests
        network_events = []
        def on_req(req):
            u = req.url.lower()
            if any(k in u for k in ["symbol", "search", "instrument", "market", "easy2_api", "mts_api"]):
                network_events.append({
                    "time": time.time(),
                    "type": "request",
                    "url": req.url,
                    "method": req.method
                })
        def on_resp(resp):
            u = resp.url.lower()
            if any(k in u for k in ["symbol", "search", "instrument", "market", "easy2_api", "mts_api"]):
                network_events.append({
                    "time": time.time(),
                    "type": "response",
                    "url": resp.url,
                    "status": resp.status
                })
        page.on("request", on_req)
        page.on("response", on_resp)

        try:
            print("[3/10] Navigating to https://m.easytrader.ir/ ...")
            page.goto("https://m.easytrader.ir/", wait_until="domcontentloaded", timeout=45000)

            # Check authentication state
            print("Checking authentication state...")
            user_selector = "#user-name, input[name='Username'], input[autocomplete='username']"
            login_form_appeared = False

            for attempt in range(15):
                page.wait_for_timeout(1000)
                current_url = page.url
                print(f"  [{attempt+1}] URL: {current_url}")

                if "login.emofid.com" in current_url or page.locator(user_selector).first.is_visible():
                    print("  -> Login page confirmed!")
                    login_form_appeared = True
                    break

                if "m.easytrader.ir" in current_url and page.locator("a[data-cy=main-navbar-search]").first.is_visible():
                    print("  -> Already on dashboard and navbar search visible!")
                    break

            if login_form_appeared:
                print("Filling credentials on login form...")
                page.locator(user_selector).first.fill(username)
                page.locator("#password, input[name='Password'], input[type='password']").first.fill(password)
                page.locator("#primary_form button[type='submit'], button[type='submit'], button:has-text('ورود')").first.click()

                print("[4/10] Waiting for redirect to EasyTrader dashboard...")
                page.wait_for_url(
                    lambda u: "m.easytrader.ir" in u and "login" not in u and "auth-callback" not in u,
                    timeout=60000
                )
                print(f"Dashboard URL reached: {page.url}")

            page.wait_for_timeout(2000)
            dismiss_any_popups(page)

            # [5/10] NAVIGATE TO SEARCH AND RUN USER-REQUESTED EXPERIMENTS
            print("\n[5/10] === COMMENCING SEARCH EXPERIMENTS (/search) ===")
            search_nav_t0 = time.time()
            page.goto("https://m.easytrader.ir/search", wait_until="domcontentloaded", timeout=25000)
            search_loaded_t = time.time()
            print(f"Navigated to /search in {search_loaded_t - search_nav_t0:.2f}s. URL: {page.url}")

            # Check and record immediate state before dismissing popups or typing
            page.wait_for_timeout(1000)
            container_state_raw = page.evaluate("""() => {
                const bodyText = document.body.innerText;
                const bottomSheet = document.querySelector('ui-overlay, .bottom-sheet-overlay, [data-cy="bottom-sheet-overlay"]');
                return {
                    bodySnippet: bodyText.slice(0, 500),
                    hasLoading: bodyText.includes('در حال بارگذاری'),
                    hasSelectedIndustries: bodyText.includes('صنایع منتخب'),
                    hasRecentHistory: bodyText.includes('تاریخچه') || bodyText.includes('جستجوهای اخیر'),
                    hasNotificationModal: bodyText.includes('مجوز اطلاع‌رسانی'),
                    hasBottomSheetOverlay: !!bottomSheet
                };
            }""")
            print(f"Raw state immediately on /search:\n{json.dumps(container_state_raw, ensure_ascii=False, indent=2)}")
            search_experiments["raw_state_on_search_page"] = container_state_raw

            # Now dismiss any bottom sheet (like "مجوز اطلاع‌رسانی" with "انصراف")
            print("Checking and dismissing any bottom sheet / popups...")
            dismissed_popups = dismiss_any_popups(page)
            search_experiments["dismissed_popups"] = dismissed_popups
            page.wait_for_timeout(1000)

            # Wait for search input to be interactable
            search_input = page.locator("input[data-cy=layout-search-input], input[placeholder*='جستجو'], #searchInputControl").first
            search_input.wait_for(state="visible", timeout=15000)

            # Check state after dismissing popups but BEFORE typing
            state_before_typing = page.evaluate("""() => {
                const bodyText = document.body.innerText;
                const resultsContainer = document.querySelector('lib-search-flat-result-list, [data-cy="search-panel-result-list"]');
                return {
                    containerFound: !!resultsContainer,
                    containerText: resultsContainer ? resultsContainer.innerText.trim() : null,
                    bodySnippet: bodyText.slice(0, 400),
                    hasSelectedIndustries: bodyText.includes('صنایع منتخب'),
                    hasRecentHistory: bodyText.includes('تاریخچه') || bodyText.includes('جستجوهای اخیر'),
                    hasLoading: bodyText.includes('در حال بارگذاری'),
                    hasEmptyMsg: bodyText.includes('نتیجه‌ای یافت نشد')
                };
            }""")
            print(f"State before typing:\n{json.dumps(state_before_typing, ensure_ascii=False, indent=2)}")
            search_experiments["state_before_typing"] = state_before_typing

            # (2) EXPERIMENT: Immediate typing test vs waiting
            # Let's type "خگلپا" character by character WITHOUT pressing Enter
            print("\n--- EXPERIMENT 2: TYPING 'خگلپا' WITHOUT ENTER ---")
            search_input.click()
            search_input.fill("")
            search_input.type("خگلپا", delay=70)
            print("Typed 'خگلپا' without Enter. Waiting 3 seconds for suggestions to populate...")
            page.wait_for_timeout(3000)

            without_enter_state = page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('a[href*="/stock-details/"], [data-cy^="search-panel-item-"], lib-search-flat-result-list a'));
                const bodyText = document.body.innerText;
                return {
                    resultsCount: links.length,
                    results: links.map(l => ({
                        text: l.innerText.trim(),
                        href: l.getAttribute('href'),
                        dataCy: l.getAttribute('data-cy')
                    })),
                    hasKhgolpa: bodyText.includes('خگلپا'),
                    hasEmptyMsg: bodyText.includes('نتیجه‌ای یافت نشد')
                };
            }""")
            print(f"State without Enter:\n{json.dumps(without_enter_state, ensure_ascii=False, indent=2)}")
            search_experiments["typing_without_enter"] = without_enter_state

            # (3) EXPERIMENT: Testing effect of Enter key
            print("\n--- EXPERIMENT 3: TESTING EFFECT OF ENTER KEY ---")
            search_input.press("Enter")
            page.wait_for_timeout(1500)
            with_enter_state = page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('a[href*="/stock-details/"], [data-cy^="search-panel-item-"], lib-search-flat-result-list a'));
                const bodyText = document.body.innerText;
                return {
                    resultsCount: links.length,
                    results: links.map(l => ({
                        text: l.innerText.trim(),
                        href: l.getAttribute('href'),
                        dataCy: l.getAttribute('data-cy')
                    })),
                    hasKhgolpa: bodyText.includes('خگلپا'),
                    hasEmptyMsg: bodyText.includes('نتیجه‌ای یافت نشد')
                };
            }""")
            print(f"State after Enter key:\n{json.dumps(with_enter_state, ensure_ascii=False, indent=2)}")
            search_experiments["typing_with_enter"] = with_enter_state

            # (4) EXPERIMENT: Clear and re-type test
            print("\n--- EXPERIMENT 4: TESTING CLEAR AND RE-TYPE ---")
            search_input.fill("")
            page.wait_for_timeout(1000)
            search_input.type("خگلپا", delay=80)
            page.wait_for_timeout(3000)
            retype_state = page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('a[href*="/stock-details/"], [data-cy^="search-panel-item-"], lib-search-flat-result-list a'));
                const bodyText = document.body.innerText;
                return {
                    resultsCount: links.length,
                    results: links.map(l => ({
                        text: l.innerText.trim(),
                        href: l.getAttribute('href'),
                        dataCy: l.getAttribute('data-cy')
                    })),
                    hasKhgolpa: bodyText.includes('خگلپا'),
                    hasEmptyMsg: bodyText.includes('نتیجه‌ای یافت نشد')
                };
            }""")
            print(f"State after clear and re-type:\n{json.dumps(retype_state, ensure_ascii=False, indent=2)}")
            search_experiments["clear_and_retype"] = retype_state

            # Locate the stock link for خگلپا
            print("\nLocating link for خگلپا in search results...")
            target_link = None
            
            # Look for link that has href with /stock-details/ and text containing خگلپا
            for cand in [without_enter_state, with_enter_state, retype_state]:
                for res in cand.get("results", []):
                    href = res.get("href")
                    txt = res.get("text", "")
                    if href and "/stock-details/" in href and ("خگلپا" in txt or "IRO5GLPA" in href):
                        print(f"Found target link with href: {txt} -> {href}")
                        target_link = page.locator(f"a[href='{href}']").first
                        break
                if target_link:
                    break

            if not target_link:
                # Direct selectors
                selectors_to_try = [
                    "a[href*='/stock-details/IRO5GLPA0001']",
                    "a[data-cy='searchPanelItem-1']",
                    "[data-cy='search-panel-item-خگلپا'] a[href*='/stock-details/']",
                    "a[href*='/stock-details/']:has-text('خگلپا')",
                    "a:has-text('خگلپا')",
                ]
                for st in selectors_to_try:
                    loc = page.locator(st).first
                    if loc.is_visible(timeout=1000):
                        target_link = loc
                        print(f"Found target link using selector: {st}")
                        break

            if not target_link:
                print("Testing search by company name 'گلپا'...")
                search_input.fill("")
                search_input.type("گلپا", delay=80)
                page.wait_for_timeout(3000)
                golpa_links = page.locator("a[href*='/stock-details/']").all()
                for gl in golpa_links:
                    txt = gl.inner_text().strip()
                    if "گلپا" in txt or "خگلپا" in txt:
                        target_link = gl
                        print(f"Found link via 'گلپا': '{txt}'")
                        break

            if not target_link:
                page.screenshot(path=r"C:\Users\Dara\Desktop\et_search_debug3.png")
                raise RuntimeError("Could not find search result for خگلپا")

            print("\n[6/10] Navigating to Stock Details Page for خگلپا...")
            target_link.click()

            # Wait for stock details page
            page.wait_for_url(lambda u: "/stock-details/" in u, timeout=25000)
            page.wait_for_timeout(4000)
            stock_url = page.url
            print(f"Stock details URL: {stock_url}")

            # Dismiss any popups on stock details page
            dismiss_any_popups(page)

            # [7/10] LOCATE STOCK BUY BUTTON
            print("\n[7/10] Locating stock buy button on stock details page...")
            buy_selectors = [
                "button[data-cy=order-buy-btn]",
                "button:has-text('خرید')",
                ".order-buy-btn",
                "[data-cy*='buy']",
            ]
            stock_buy_btn = None
            found_buy_selector = None
            for bs in buy_selectors:
                try:
                    el = page.locator(bs).first
                    if el.is_visible(timeout=2000):
                        stock_buy_btn = el
                        found_buy_selector = bs
                        print(f"Found stock buy button: {bs}")
                        break
                except Exception:
                    pass

            if not stock_buy_btn:
                # Log all buttons on page
                all_b = page.evaluate("() => Array.from(document.querySelectorAll('button')).map(b => ({text: b.innerText, dcy: b.getAttribute('data-cy'), cls: b.className}))")
                print(f"All buttons on page: {all_b}")
                raise RuntimeError("Stock buy button not found on page")

            url_before_buy = page.url
            print("CLICKING STOCK BUY BUTTON TO OPEN ORDER FORM...")
            stock_buy_btn.click()

            print("Waiting 3.5 seconds for drawer / bottom sheet animation...")
            page.wait_for_timeout(3500)

            url_after_buy = page.url
            url_changed = (url_before_buy != url_after_buy)
            print(f"URL transition: {url_before_buy} -> {url_after_buy} (changed={url_changed})")

            # [8/10] COMPREHENSIVE ORDER FORM DOM AUDIT
            print("\n[8/10] Running comprehensive DOM audit on order form...")
            dom_report = page.evaluate("""() => {
                function getRootNodeInfo(el) {
                    const root = el.getRootNode();
                    return {
                        isShadow: root instanceof ShadowRoot,
                        rootName: root.constructor.name
                    };
                }

                function getElementDetails(el) {
                    if (!el) return null;
                    const rect = el.getBoundingClientRect();
                    const cs = window.getComputedStyle(el);
                    const attrs = {};
                    for (let a of el.attributes) {
                        attrs[a.name] = a.value;
                    }
                    const shadowInfo = getRootNodeInfo(el);
                    return {
                        tagName: el.tagName.toLowerCase(),
                        id: el.id || null,
                        className: (typeof el.className === 'string') ? el.className : null,
                        dataCy: el.getAttribute('data-cy') || null,
                        type: el.getAttribute('type') || null,
                        name: el.getAttribute('name') || null,
                        placeholder: el.getAttribute('placeholder') || null,
                        value: el.value !== undefined ? el.value : null,
                        innerText: (el.innerText || '').trim(),
                        attributes: attrs,
                        rect: {
                            x: Math.round(rect.x),
                            y: Math.round(rect.y),
                            width: Math.round(rect.width),
                            height: Math.round(rect.height),
                            top: Math.round(rect.top),
                            left: Math.round(rect.left)
                        },
                        visible: rect.width > 0 && rect.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0',
                        disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
                        readonly: el.readOnly || el.getAttribute('readonly') !== null,
                        pointerEvents: cs.pointerEvents,
                        display: cs.display,
                        visibility: cs.visibility,
                        opacity: cs.opacity,
                        zIndex: cs.zIndex,
                        position: cs.position,
                        insideShadow: shadowInfo.isShadow,
                        insideIframe: window.self !== window.top,
                        parentTagName: el.parentElement ? el.parentElement.tagName.toLowerCase() : null,
                        parentClassName: el.parentElement ? ((typeof el.parentElement.className === 'string') ? el.parentElement.className : '') : null,
                        parentDataCy: el.parentElement ? el.parentElement.getAttribute('data-cy') : null,
                        outerHTML: el.outerHTML
                    };
                }

                const containerCandidates = [
                    'lib-order-form',
                    'mat-bottom-sheet-container',
                    '.mat-bottom-sheet-container',
                    '.cdk-overlay-pane',
                    '[data-cy*="order-form"]',
                    '.order-form',
                    'easy-order-form',
                    'app-order-form',
                    '.bottom-sheet',
                    '[class*="order-form"]'
                ];
                let activeContainerEl = null;
                let activeContainerSel = null;
                for (let c of containerCandidates) {
                    const el = document.querySelector(c);
                    if (el && el.offsetHeight > 0) {
                        activeContainerEl = el;
                        activeContainerSel = c;
                        break;
                    }
                }

                const allInputs = Array.from(document.querySelectorAll('input')).map(getElementDetails);
                const allButtons = Array.from(document.querySelectorAll('button, [role="button"]')).map(getElementDetails);

                const qtyCandidates = Array.from(document.querySelectorAll('input')).filter(inp => {
                    const s = `${inp.getAttribute('data-cy')} ${inp.id} ${inp.name} ${inp.placeholder} ${inp.className}`;
                    return /quantity|qty|volume|تعداد|حجم/i.test(s);
                }).map(getElementDetails);

                const priceCandidates = Array.from(document.querySelectorAll('input')).filter(inp => {
                    const s = `${inp.getAttribute('data-cy')} ${inp.id} ${inp.name} ${inp.placeholder} ${inp.className}`;
                    return /price|قیمت/i.test(s);
                }).map(getElementDetails);

                const maxPriceCandidates = Array.from(document.querySelectorAll('*')).filter(el => {
                    const dcy = el.getAttribute('data-cy') || '';
                    const cls = (typeof el.className === 'string') ? el.className : '';
                    const title = el.getAttribute('title') || '';
                    const aria = el.getAttribute('aria-label') || '';
                    const s = `${dcy} ${cls} ${title} ${aria}`;
                    return /max-price|max-btn|order-form-max|سقف|spin-box/i.test(s);
                }).map(getElementDetails);

                const submitBuyCandidates = Array.from(document.querySelectorAll('button, [role="button"]')).filter(b => {
                    const dcy = b.getAttribute('data-cy') || '';
                    const cls = (typeof b.className === 'string') ? b.className : '';
                    const txt = (b.innerText || '').trim();
                    const s = `${dcy} ${cls} ${txt}`;
                    return /submit|buy|ارسال|ثبت/i.test(s);
                }).map(getElementDetails);

                const backdrops = Array.from(document.querySelectorAll('.cdk-overlay-backdrop, .modal-backdrop, [class*="backdrop"], [class*="overlay-backdrop"], .bottom-sheet-overlay')).map(getElementDetails);

                const dataCyElements = Array.from(document.querySelectorAll('[data-cy]')).map(el => ({
                    dataCy: el.getAttribute('data-cy'),
                    tagName: el.tagName.toLowerCase(),
                    id: el.id || null,
                    className: (typeof el.className === 'string') ? el.className : null,
                    innerText: (el.innerText || '').slice(0, 100).trim(),
                    visible: el.offsetWidth > 0 && el.offsetHeight > 0,
                    rect: el.getBoundingClientRect()
                }));

                let containerOuterHTML = "";
                if (activeContainerEl) {
                    containerOuterHTML = activeContainerEl.outerHTML;
                } else {
                    const formEl = document.querySelector('form') || document.querySelector('[class*="order"]') || document.querySelector('.cdk-overlay-container');
                    containerOuterHTML = formEl ? formEl.outerHTML : document.body.innerHTML;
                }

                return {
                    activeContainerSel,
                    containerOuterHTML,
                    qtyCandidates,
                    priceCandidates,
                    maxPriceCandidates,
                    submitBuyCandidates,
                    backdrops,
                    allInputs,
                    allButtons,
                    dataCyElements,
                    frameCount: window.frames.length
                };
            }""")

            print(f"Active container identified: {dom_report['activeContainerSel']}")
            print(f"Total inputs: {len(dom_report['allInputs'])}, total buttons: {len(dom_report['allButtons'])}")
            print(f"Quantity candidates: {len(dom_report['qtyCandidates'])}")
            print(f"Price candidates: {len(dom_report['priceCandidates'])}")
            print(f"Max-price candidates: {len(dom_report['maxPriceCandidates'])}")
            print(f"Submit candidates: {len(dom_report['submitBuyCandidates'])}")

            with open(html_output_path, "w", encoding="utf-8") as f:
                f.write(dom_report["containerOuterHTML"])
            print(f"Wrote order form outerHTML to {html_output_path} (size: {len(dom_report['containerOuterHTML'])} chars)")

            # [9/10] HARLESS WRITABILITY TEST: Fill '1' in quantity input
            print("\n[9/10] Testing quantity field writability with '1'...")
            writability_verdict = {"tested": False, "success": False, "value_after": None, "selector": None, "error": None}

            candidate_selectors = []
            for qc in dom_report['qtyCandidates']:
                if qc.get('dataCy'):
                    candidate_selectors.append(f"[data-cy='{qc['dataCy']}']")
                if qc.get('id'):
                    candidate_selectors.append(f"#{qc['id']}")

            candidate_selectors.extend([
                "input[data-cy=order-form-input-quantity]",
                "input#quantity",
                "input[placeholder*='تعداد']",
                "input[placeholder*='حجم']",
                "input[name*='quantity' i]",
            ])

            target_qty_loc = None
            target_qty_sel = None
            for c_sel in candidate_selectors:
                try:
                    l = page.locator(c_sel).first
                    if l.is_visible(timeout=1000):
                        target_qty_loc = l
                        target_qty_sel = c_sel
                        print(f"Found visible quantity locator with: {c_sel}")
                        break
                except Exception:
                    pass

            if target_qty_loc:
                try:
                    print(f"Attempting fill('1') on {target_qty_sel}...")
                    target_qty_loc.fill("1")
                    page.wait_for_timeout(500)
                    val = target_qty_loc.input_value()
                    print(f"Value after fill: '{val}'")
                    writability_verdict["tested"] = True
                    writability_verdict["selector"] = target_qty_sel
                    writability_verdict["value_after"] = val
                    writability_verdict["success"] = ("1" in val)
                except Exception as e:
                    print(f"Error during fill: {e}")
                    writability_verdict["error"] = str(e)
            else:
                print("WARNING: Could not find visible quantity locator to test fill!")
                writability_verdict["error"] = "No visible quantity locator found"

            # Check interaction requirements on max-price arrow
            print("Checking max price arrow behavior & interaction requirements...")
            max_arrow_check = page.evaluate("""() => {
                const arrow = document.querySelector('[data-cy="order-form-max-price"], .spin-box.order-form-max-btn, .order-form-max-btn, [data-cy*="max-price"]');
                const price = document.querySelector('input[data-cy="order-form-input-price"], input[placeholder*="قیمت"]');
                if (!arrow) return { exists: false };
                const rect = arrow.getBoundingClientRect();
                const cs = window.getComputedStyle(arrow);
                return {
                    exists: true,
                    tagName: arrow.tagName.toLowerCase(),
                    dataCy: arrow.getAttribute('data-cy'),
                    className: arrow.className,
                    rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
                    visible: rect.width > 0 && rect.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden',
                    pointerEvents: cs.pointerEvents,
                    cursor: cs.cursor,
                    priceValueBefore: price ? price.value : null
                };
            }""")
            print(f"Max arrow check: {json.dumps(max_arrow_check, ensure_ascii=False, indent=2)}")

            # Screenshot
            print(f"Taking screenshot to {png_output_path} ...")
            page.screenshot(path=str(png_output_path), full_page=False)
            print("Screenshot saved!")

            # Write TXT report
            print(f"Writing audit report to {txt_output_path} ...")
            with open(txt_output_path, "w", encoding="utf-8") as f:
                f.write("=====================================================\n")
                f.write("   EASYTRADER LIVE ORDER FORM AUDIT REPORT (خگلپا)   \n")
                f.write("=====================================================\n\n")
                f.write(f"Stock Details URL: {stock_url}\n")
                f.write(f"URL Before Buy Click: {url_before_buy}\n")
                f.write(f"URL After Buy Click: {url_after_buy} (Changed: {url_changed})\n")
                f.write(f"Stock Buy Button Selector: {found_buy_selector}\n")
                f.write(f"Active Order Form Container: {dom_report['activeContainerSel']}\n\n")

                f.write("-----------------------------------------------------\n")
                f.write("SEARCH EXPERIMENT FINDINGS (USER SPECIAL QUESTIONS)\n")
                f.write("-----------------------------------------------------\n")
                f.write(f"{json.dumps(search_experiments, ensure_ascii=False, indent=2)}\n\n")

                f.write("-----------------------------------------------------\n")
                f.write("1. QUANTITY FIELD (تعداد / حجم)\n")
                f.write("-----------------------------------------------------\n")
                f.write(f"Writability Verdict: {json.dumps(writability_verdict, ensure_ascii=False, indent=2)}\n")
                f.write(f"Discovered Elements:\n{json.dumps(dom_report['qtyCandidates'], ensure_ascii=False, indent=2)}\n\n")

                f.write("-----------------------------------------------------\n")
                f.write("2. PRICE FIELD (قیمت)\n")
                f.write("-----------------------------------------------------\n")
                f.write(f"Discovered Elements:\n{json.dumps(dom_report['priceCandidates'], ensure_ascii=False, indent=2)}\n\n")

                f.write("-----------------------------------------------------\n")
                f.write("3. MAX PRICE ARROW / BUTTON (سقف قیمت / فلش)\n")
                f.write("-----------------------------------------------------\n")
                f.write(f"Interaction Check:\n{json.dumps(max_arrow_check, ensure_ascii=False, indent=2)}\n")
                f.write(f"Discovered Elements:\n{json.dumps(dom_report['maxPriceCandidates'], ensure_ascii=False, indent=2)}\n\n")

                f.write("-----------------------------------------------------\n")
                f.write("4. SUBMIT BUY BUTTON (ارسال خرید - NEVER CLICKED)\n")
                f.write("-----------------------------------------------------\n")
                f.write(f"Discovered Elements:\n{json.dumps(dom_report['submitBuyCandidates'], ensure_ascii=False, indent=2)}\n\n")

                f.write("-----------------------------------------------------\n")
                f.write("5. OVERLAYS & BACKDROPS\n")
                f.write("-----------------------------------------------------\n")
                f.write(f"{json.dumps(dom_report['backdrops'], ensure_ascii=False, indent=2)}\n\n")

                f.write("-----------------------------------------------------\n")
                f.write("6. ALL DATA-CY ELEMENTS IN ACTIVE VIEW\n")
                f.write("-----------------------------------------------------\n")
                f.write(f"{json.dumps(dom_report['dataCyElements'], ensure_ascii=False, indent=2)}\n\n")

                f.write("-----------------------------------------------------\n")
                f.write("7. ALL INPUTS IN DOM\n")
                f.write("-----------------------------------------------------\n")
                f.write(f"{json.dumps(dom_report['allInputs'], ensure_ascii=False, indent=2)}\n\n")

                f.write("-----------------------------------------------------\n")
                f.write("8. ALL BUTTONS IN DOM\n")
                f.write("-----------------------------------------------------\n")
                f.write(f"{json.dumps(dom_report['allButtons'], ensure_ascii=False, indent=2)}\n")

            print("Report written successfully!")

        except Exception as ex:
            print(f"FATAL ERROR OCCURRED: {ex}")
            traceback.print_exc()
            try:
                page.screenshot(path=str(err_output_path), full_page=False)
                print(f"Captured fatal error screenshot to {err_output_path}")
            except Exception:
                pass
            raise

        finally:
            print("Cleaning up browser context...")
            try:
                context.close()
            except Exception:
                pass

    print("ALL STEPS FINISHED CLEANLY.")

if __name__ == "__main__":
    main()
