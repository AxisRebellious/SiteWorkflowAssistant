
import asyncio
from playwright.async_api import async_playwright

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://www.mtcaptcha.com/test-multiple-captcha", wait_until="networkidle")
        await asyncio.sleep(2)
        
        frames = [f for f in page.frames if "mtcaptcha" in f.url]
        print("Frames:", len(frames))
        if frames:
            f = frames[0]
            inp = f.locator('input[type="text"]').first
            await inp.click()
            await inp.fill("WRONG12") # 7 chars
            await inp.evaluate("(el) => { el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }")
            
            await asyncio.sleep(2)
            html = await f.content()
            if "error" in html.lower() or "fail" in html.lower() or "invalid" in html.lower():
                print("Error detected in HTML")
            
            # Check if input is cleared or class changed
            val = await inp.input_value()
            print("Input value after 2s:", val)
            
        await browser.close()

asyncio.run(test())
