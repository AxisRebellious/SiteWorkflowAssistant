import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import automation


class TestFallbackSelectors(unittest.TestCase):
    def test_resolve_selector_candidates_single(self):
        cands = automation._resolve_selector_candidates("#btn-login")
        self.assertEqual(cands, ["#btn-login"])

    def test_resolve_selector_candidates_list(self):
        cands = automation._resolve_selector_candidates(
            "#btn-old",
            ["#btn-new", ".login-button", "button[type='submit']"],
        )
        self.assertEqual(
            cands,
            ["#btn-new", ".login-button", "button[type='submit']", "#btn-old"],
        )

    def test_resolve_selector_candidates_dedup(self):
        cands = automation._resolve_selector_candidates(
            "#btn-new",
            ["#btn-new", ".login-button"],
        )
        self.assertEqual(cands, ["#btn-new", ".login-button"])

    def test_resolve_selector_candidates_empty_or_none(self):
        cands = automation._resolve_selector_candidates("", ["  #btn1  ", "", None, "#btn2"])
        self.assertEqual(cands, ["#btn1", "#btn2"])


class TestAsyncSelectorFallback(unittest.IsolatedAsyncioTestCase):
    async def test_first_visible_locator_fallback(self):
        page = MagicMock()
        page.frames = []

        loc_hidden = AsyncMock()
        loc_hidden.is_visible = AsyncMock(return_value=False)

        loc_visible = AsyncMock()
        loc_visible.is_visible = AsyncMock(return_value=True)

        def mock_locator(sel):
            mock = MagicMock()
            if sel == "#hidden-sel":
                mock.first = loc_hidden
            elif sel == "#visible-sel":
                mock.first = loc_visible
            else:
                m = AsyncMock()
                m.is_visible = AsyncMock(return_value=False)
                mock.first = m
            return mock

        page.locator = mock_locator

        res = await automation._playback_first_visible_locator(
            page,
            selector="#hidden-sel",
            timeout_ms=500,
            selectors=["#hidden-sel", "#visible-sel"],
        )
        self.assertEqual(res, loc_visible)


class TestDynamicEntry(unittest.TestCase):
    def test_scenario_entry_url_priority(self):
        sc = {
            "start_url": "https://account.emofid.com/login?token=old123",
            "entry_url_base": "https://m.easytrader.ir",
            "entry_selector": "#btn-login",
        }
        res = automation._resolve_scenario_entry_url(sc, None)
        self.assertEqual(res, "https://m.easytrader.ir")

    def test_playback_steps_with_entry_base(self):
        sc = {
            "start_url": "https://account.emofid.com/login?token=old123",
            "entry_url_base": "https://m.easytrader.ir",
            "entry_selector": "#btn-login",
            "steps": [
                {"id": "s0", "type": "goto", "url": "https://account.emofid.com/login?token=old123"}
            ],
        }
        steps = automation.playback_steps_with_entry(sc, playback_start_url=None)
        self.assertEqual(steps[0]["url"], "https://m.easytrader.ir")


class TestDeadUrlCap(unittest.IsolatedAsyncioTestCase):
    async def test_goto_resilient_caps_dead_url(self):
        page = MagicMock()
        attempts = [0]

        async def fail_goto(*args, **kwargs):
            attempts[0] += 1
            raise Exception("net::ERR_CONNECTION_REFUSED")

        with patch("automation._goto_playback_once", side_effect=fail_goto):
            with self.assertRaises(Exception):
                await automation._goto_playback_resilient(
                    page,
                    "http://dead-url-test.com",
                    timeout_nav=1000,
                    max_attempts=None,
                    pause_between_attempts_s=0.01,
                )
            self.assertEqual(attempts[0], automation.DEFAULT_NAV_RETRY_DEAD_URL_CAP)

    async def test_permanent_net_error_fails_fast(self):
        page = MagicMock()
        attempts = [0]

        async def fail_dns(*args, **kwargs):
            attempts[0] += 1
            raise Exception("net::ERR_NAME_NOT_RESOLVED")

        with patch("automation._goto_playback_once", side_effect=fail_dns):
            with self.assertRaises(Exception):
                await automation._goto_playback_resilient(
                    page,
                    "http://non-existent-domain-498234.ir",
                    timeout_nav=1000,
                    max_attempts=None,
                    pause_between_attempts_s=0.01,
                )
            # Permanent network error should fail on attempt 1 without endless retrying!
            self.assertEqual(attempts[0], 1)


class TestRunScenarioStepsIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_run_scenario_steps_dynamic_entry_and_fallback_selectors(self):
        browser = MagicMock()
        context = MagicMock()
        context.pages = []
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        page.url = "https://m.easytrader.ir"
        page.frames = []
        page_holder = [page]

        clicked_selectors = []
        filled_values = {}

        loc_btn1 = AsyncMock()
        loc_btn1.is_visible = AsyncMock(return_value=False)
        loc_btn2 = AsyncMock()
        loc_btn2.is_visible = AsyncMock(return_value=True)

        async def mock_btn_click(*args, **kwargs):
            clicked_selectors.append("#btn-fallback")

        loc_btn2.click = mock_btn_click

        loc_inp1 = AsyncMock()
        loc_inp1.is_visible = AsyncMock(return_value=False)
        loc_inp2 = AsyncMock()
        loc_inp2.is_visible = AsyncMock(return_value=True)
        loc_inp2.input_value = AsyncMock(return_value="09123456789")

        async def mock_inp_fill(val, *args, **kwargs):
            filled_values["mobile"] = val

        loc_inp2.fill = mock_inp_fill

        loc_entry_btn = AsyncMock()
        loc_entry_btn.is_visible = AsyncMock(return_value=True)

        async def mock_entry_click(*args, **kwargs):
            clicked_selectors.append("#entry-login-btn")

        loc_entry_btn.click = mock_entry_click

        def mock_locator(sel):
            mock = MagicMock()
            if sel == "#btn-primary":
                mock.first = loc_btn1
            elif sel == "#btn-fallback":
                mock.first = loc_btn2
            elif sel == "#mobile-old":
                mock.first = loc_inp1
            elif sel == "input[name='mobile']":
                mock.first = loc_inp2
            elif sel == "#entry-login-btn":
                mock.first = loc_entry_btn
            else:
                m = AsyncMock()
                m.is_visible = AsyncMock(return_value=False)
                mock.first = m
            return mock

        page.locator = mock_locator

        scenario = {
            "entry_url_base": "https://m.easytrader.ir",
            "entry_selector": "#entry-login-btn",
        }

        steps = [
            {
                "id": "goto_1",
                "type": "goto",
                "url": "https://account.emofid.com/login?token=dead123",
                # Should use entry_url_base and click entry-login-btn
            },
            {
                "id": "fill_1",
                "type": "fill",
                "selector": "#mobile-old",
                "selectors": ["#mobile-old", "input[name='mobile']"],
                "value": "09123456789",
            },
            {
                "id": "clk_1",
                "type": "click",
                "selector": "#btn-primary",
                "selectors": ["#btn-primary", "#btn-fallback"],
            },
        ]

        with patch("automation._goto_playback_once", new_callable=AsyncMock) as mock_goto:
            log_lines = await automation.run_scenario_steps(
                browser,
                context,
                page_holder,
                steps,
                task_values={},
                scenario=scenario,
                defaults={},
                goto_retry_max_attempts=2,
                goto_retry_pause_s=0.01,
                goto_per_attempt_timeout_ms=1000,
                auto_solve_captcha=False,
            )

            # 1. Verify goto went to entry_url_base ("https://m.easytrader.ir") instead of dead deep URL
            mock_goto.assert_called_once()
            called_url = mock_goto.call_args[0][1]
            self.assertEqual(called_url, "https://m.easytrader.ir")

            # 2. Verify entry_click was clicked
            self.assertIn("#entry-login-btn", clicked_selectors)

            # 3. Verify fallback fill worked
            self.assertEqual(filled_values.get("mobile"), "09123456789")

            # 4. Verify fallback click worked
            self.assertIn("#btn-fallback", clicked_selectors)


if __name__ == "__main__":
    unittest.main()
