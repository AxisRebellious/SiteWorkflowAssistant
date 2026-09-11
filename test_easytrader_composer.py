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
        self.assertFalse(sc.get("keep_browser_open", False))
        step_ids = [s["id"] for s in sc["steps"]]
        self.assertNotIn("clk_final_submit_buy", step_ids)
        self.assertIn("submit_until_filled", step_ids)
        final_step = next(s for s in sc["steps"] if s["id"] == "submit_until_filled")
        self.assertEqual(final_step["type"], "submit_until_confirmed")
        self.assertTrue(final_step.get("no_resume"))
        self.assertIn("button[data-cy=oms-order-form-submit-button-buy]", final_step["selectors"])
        self.assertEqual(final_step["qty_value"], "1000")
        self.assertGreaterEqual(final_step["max_attempts"], 1)

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


if __name__ == "__main__":
    unittest.main()
