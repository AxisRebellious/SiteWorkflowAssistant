import sys
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import automation
import main
from main import compose_easytrader_scenario, EasyTraderBuyBody


class TestEasyTraderComposer(unittest.TestCase):
    def test_dry_run_composition(self):
        sc = compose_easytrader_scenario(
            username="user123",
            password="pass123",
            stock="فولاد",
            qty=500,
            dry_run=True,
            turbo_mode=True,
        )
        self.assertTrue(sc["keep_browser_open"])
        self.assertTrue(sc["reuse_browser"])
        self.assertTrue(sc["turbo_mode"])
        
        step_ids = [s["id"] for s in sc["steps"]]
        self.assertNotIn("clk_final_submit_buy", step_ids)
        self.assertNotIn("submit_until_filled", step_ids)
        self.assertIn("clk_max_price_arrow", step_ids)
        self.assertIn("fill_order_qty", step_ids)
        self.assertIn("clk_close_popup", step_ids)

        popup_step = next(s for s in sc["steps"] if s["id"] == "clk_close_popup")
        self.assertTrue(popup_step["optional"])
        self.assertIn("[data-cy=cancel-action-confirm-btn]", popup_step["selectors"])
        self.assertEqual(popup_step["timeout_ms"], 1500)

        # verify_login comes before popup to eliminate auth-callback idle delay
        step_ids = [s["id"] for s in sc["steps"]]
        self.assertLess(step_ids.index("verify_login"), step_ids.index("clk_close_popup"))
        v_step = next(s for s in sc["steps"] if s["id"] == "verify_login")
        self.assertIn("auth-callback", v_step["not_contains"])

        # search typing configuration
        search_step = next(s for s in sc["steps"] if s["id"] == "fill_stock_search")
        self.assertTrue(search_step.get("type_text"))
        self.assertEqual(search_step.get("char_delay_ms"), 20)
        self.assertFalse(search_step.get("press_enter"))

        # search result selectors
        res_step = next(s for s in sc["steps"] if s["id"] == "clk_first_search_result")
        self.assertIn("[data-cy='search-panel-item-فولاد'] a[href*='/stock-details/']", res_step["selectors"])

        qty_step = next(s for s in sc["steps"] if s["id"] == "fill_order_qty")
        self.assertEqual(qty_step["value"], "500")

    def test_real_buy_composition(self):
        sc = compose_easytrader_scenario(
            username="user123",
            password="pass123",
            stock="خودرو",
            qty="1000",
            dry_run=False,
            turbo_mode=True,
        )
        self.assertTrue(sc.get("keep_browser_open", False))
        self.assertTrue(sc.get("reuse_browser", False))
        step_ids = [s["id"] for s in sc["steps"]]
        self.assertNotIn("clk_final_submit_buy", step_ids)
        self.assertIn("submit_until_filled", step_ids)
        final_step = next(s for s in sc["steps"] if s["id"] == "submit_until_filled")
        self.assertEqual(final_step["type"], "submit_until_confirmed")
        self.assertTrue(final_step.get("no_resume"))
        self.assertIn("button[data-cy=oms-order-form-submit-button-buy]", final_step["selectors"])
        self.assertEqual(final_step["qty_value"], "1000")
        self.assertGreaterEqual(final_step["max_attempts"], 1)

    def test_target_epoch_composition(self):
        test_epoch = 1770000000.0
        sc = compose_easytrader_scenario(
            username="user123",
            password="pass123",
            stock="فولاد",
            qty="200",
            dry_run=False,
            turbo_mode=True,
            target_epoch=test_epoch,
        )
        sub_step = next(s for s in sc["steps"] if s["id"] == "submit_until_filled")
        self.assertEqual(sub_step.get("target_epoch"), test_epoch)

    def test_login_and_nav_conditional_skip_attributes(self):
        sc = compose_easytrader_scenario(
            username="user123",
            password="pass123",
            stock="شستا",
            qty="50",
            dry_run=False,
        )
        goto_step = next(s for s in sc["steps"] if s["id"] == "goto_easytrader")
        self.assertEqual(goto_step.get("skip_if_url_contains"), "m.easytrader.ir")

        login_step_ids = ["fill_username", "fill_password", "clk_login_submit", "verify_login"]
        for sid in login_step_ids:
            st = next(s for s in sc["steps"] if s["id"] == sid)
            self.assertEqual(st.get("only_if_url_contains"), "login.emofid.com", f"Step {sid} missing only_if_url_contains")

        search_nav_step = next(s for s in sc["steps"] if s["id"] == "clk_search_nav")
        self.assertEqual(search_nav_step.get("skip_if_url_contains"), "/search")
        self.assertEqual(search_nav_step.get("selector"), "a[data-cy=main-navbar-search]")

    def test_parse_target_epoch(self):
        from main import parse_target_epoch
        # Explicit target epoch float
        self.assertEqual(parse_target_epoch(target_epoch=123456.78), 123456.78)
        # Empty string
        self.assertIsNone(parse_target_epoch(target_time=""))
        self.assertIsNone(parse_target_epoch(target_time="  "))
        # Time HH:MM:SS
        epoch = parse_target_epoch(target_time="08:45:00")
        self.assertIsNotNone(epoch)
        self.assertIsInstance(epoch, float)
        # ISO string
        iso_epoch = parse_target_epoch(target_time="2026-09-19T08:45:00")
        self.assertIsNotNone(iso_epoch)

    def test_easytrader_buy_body_target_fields(self):
        body = EasyTraderBuyBody(
            username="user",
            password="pwd",
            stock="ذوب",
            target_time="08:45:00",
            target_epoch=1770000000.0,
        )
        self.assertEqual(body.target_time, "08:45:00")
        self.assertEqual(body.target_epoch, 1770000000.0)

    def test_stop_at_search_composition(self):
        sc = compose_easytrader_scenario(
            username="user123",
            password="pass123",
            stock="فولاد",
            qty="500",
            dry_run=True,
            turbo_mode=True,
            stop_at_search=True,
        )
        step_ids = [s["id"] for s in sc["steps"]]
        self.assertIn("clk_first_search_result", step_ids)
        self.assertNotIn("clk_stock_buy", step_ids)
        self.assertNotIn("fill_order_qty", step_ids)
        self.assertNotIn("clk_max_price_arrow", step_ids)
        self.assertNotIn("clk_final_submit_buy", step_ids)
        self.assertNotIn("submit_until_filled", step_ids)

    def test_custom_price_composition(self):
        sc_max = compose_easytrader_scenario(
            username="user123", password="pass123", stock="خگلپا", qty="1",
            dry_run=True, turbo_mode=True,
        )
        ids_max = [s["id"] for s in sc_max["steps"]]
        self.assertIn("clk_max_price_arrow", ids_max)
        self.assertNotIn("fill_custom_price", ids_max)

        sc = compose_easytrader_scenario(
            username="user123", password="pass123", stock="خگلپا", qty="1",
            dry_run=True, turbo_mode=True, price_mode="custom", price_value="4270",
        )
        ids = [s["id"] for s in sc["steps"]]
        self.assertIn("fill_custom_price", ids)
        self.assertNotIn("clk_max_price_arrow", ids)
        fp = next(s for s in sc["steps"] if s["id"] == "fill_custom_price")
        self.assertEqual(fp["value"], "4270")
        self.assertIn("input[data-cy=order-form-input-price]", fp["selectors"])

        sc_real = compose_easytrader_scenario(
            username="user123", password="pass123", stock="خگلپا", qty="1",
            dry_run=False, turbo_mode=True, price_mode="custom", price_value="4270",
        )
        loop = next(s for s in sc_real["steps"] if s["id"] == "submit_until_filled")
        self.assertEqual(loop["price_value"], "4270")
        self.assertEqual(loop["max_selector"], "")

    def test_routes_exist(self):
        routes = [r.path for r in main.app.routes]
        self.assertIn("/easytrader", routes)
        self.assertIn("/api/easytrader/buy", routes)
        self.assertIn("/api/easytrader/cancel", routes)
        self.assertIn("/api/easytrader/logs", routes)
        self.assertIn("/api/easytrader/reset_session", routes)


class TestOptionalStepExecution(unittest.IsolatedAsyncioTestCase):
    async def test_optional_click_step_does_not_fail_on_missing_element(self):
        browser = MagicMock()
        browser.is_connected = MagicMock(return_value=True)
        context = MagicMock()
        context.pages = []
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        page.url = "https://m.easytrader.ir"
        page.frames = []
        page_holder = [page]

        # Locator that fails with TimeoutError
        failing_loc = MagicMock()
        failing_loc.is_visible = AsyncMock(return_value=False)
        failing_loc.wait_for = AsyncMock(side_effect=TimeoutError("Element not found"))
        page.locator = MagicMock(return_value=MagicMock(first=failing_loc))

        steps = [
            {
                "id": "clk_optional_popup",
                "type": "click",
                "selector": "#missing-popup-btn",
                "optional": True,
                "timeout_ms": 1000,
            }
        ]

        # Should NOT raise, but log that optional step was skipped
        logs = await automation.run_scenario_steps(
            browser,
            context,
            page_holder,
            steps,
            {},
            scenario={},
            defaults={},
        )
        self.assertTrue(any("اختیاری رد شد" in line for line in logs))

    async def test_conditional_step_skip_only_if_url(self):
        browser = MagicMock()
        browser.is_connected = MagicMock(return_value=True)
        context = MagicMock()
        context.pages = []
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        page.url = "https://m.easytrader.ir/market-watch"
        page_holder = [page]

        steps = [
            {
                "id": "login_step",
                "type": "fill",
                "only_if_url_contains": "login.emofid.com",
                "selector": "#user-name",
                "value": "myuser",
            }
        ]

        logs = await automation.run_scenario_steps(
            browser,
            context,
            page_holder,
            steps,
            {},
            scenario={},
            defaults={},
        )
        self.assertTrue(any("قدم login_step رد شد (شرط آدرس)" in line for line in logs))

    async def test_conditional_step_skip_skip_if_url(self):
        browser = MagicMock()
        browser.is_connected = MagicMock(return_value=True)
        context = MagicMock()
        context.pages = []
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        page.url = "https://m.easytrader.ir/search"
        page_holder = [page]

        steps = [
            {
                "id": "goto_step",
                "type": "goto",
                "skip_if_url_contains": "m.easytrader.ir",
                "url": "https://m.easytrader.ir/",
            }
        ]

        logs = await automation.run_scenario_steps(
            browser,
            context,
            page_holder,
            steps,
            {},
            scenario={},
            defaults={},
        )
        self.assertTrue(any("قدم goto_step رد شد (شرط آدرس)" in line for line in logs))

    async def test_reset_shared_playback_context(self):
        mock_ctx = MagicMock()
        mock_ctx.close = AsyncMock()
        automation._playback_shared_context = mock_ctx
        closed = await automation.reset_shared_playback_context()
        self.assertTrue(closed)
        self.assertIsNone(automation._playback_shared_context)
        mock_ctx.close.assert_awaited_once()

    async def test_submit_until_confirmed_timer_and_rapid_retry(self):
        import time
        browser = MagicMock()
        browser.is_connected = MagicMock(return_value=True)
        context = MagicMock()
        context.pages = []
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        page.url = "https://m.easytrader.ir/order-form/123"
        page_holder = [page]

        submit_loc = MagicMock()
        submit_loc.is_visible = AsyncMock(return_value=True)
        submit_loc.click = AsyncMock()
        page.locator = MagicMock(return_value=MagicMock(first=submit_loc))
        page.wait_for_load_state = AsyncMock()

        # Attempt 1: body has "خارج از ساعت معاملات"
        # Attempt 2: body has "سفارش شما با موفقیت ثبت شد"
        eval_responses = [
            "خارج از ساعت معاملات",
            "سفارش شما با موفقیت ثبت شد",
        ]
        page.evaluate = AsyncMock(side_effect=lambda expr, *args: eval_responses.pop(0) if eval_responses else "ثبت شد")

        target = time.time() + 0.05

        steps = [
            {
                "id": "submit_test",
                "type": "submit_until_confirmed",
                "selector": "#btn-submit",
                "target_epoch": target,
                "wait_s": 45,
                "max_attempts": 3,
            }
        ]

        with patch("automation._sleep_cancellable", new_callable=AsyncMock) as mock_sleep:
            logs = await automation.run_scenario_steps(
                browser,
                context,
                page_holder,
                steps,
                {},
                scenario={},
                defaults={},
            )

            self.assertTrue(any("سفارش ثبت شد" in line for line in logs))
            sleep_durations = [c.args[0] for c in mock_sleep.call_args_list]
            self.assertIn(0.4, sleep_durations)


if __name__ == "__main__":
    unittest.main()
