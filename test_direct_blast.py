import asyncio
import json
import sys
import time
from email.utils import formatdate
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from direct_blast import (
    PRE_WARM_WINDOW_SECONDS,
    DirectBlastEngine,
    calculate_fire_epoch,
    calculate_prewarm_epoch,
    compile_order_payload,
    blast_until_accepted,
)


class TestPayloadCompilation(unittest.TestCase):
    def test_compile_order_payload_returns_bytes(self):
        payload = compile_order_payload("فولاد", 100, 5000)
        self.assertIsInstance(payload, bytes)

    def test_compile_order_payload_contents(self):
        payload_bytes = compile_order_payload(
            symbol="شستا",
            qty="500",
            price="1200",
            order_side="buy",
            is_max_price=True,
        )
        data = json.loads(payload_bytes.decode("utf-8"))
        self.assertEqual(data["symbol"], "شستا")
        self.assertEqual(data["quantity"], 500)
        self.assertEqual(data["price"], 1200)
        self.assertEqual(data["order_side"], "buy")
        self.assertTrue(data["is_max_price"])

    def test_compile_order_payload_persian_utf8(self):
        symbol = "خودرو"
        payload_bytes = compile_order_payload(symbol, 10, 2500)
        decoded = json.loads(payload_bytes.decode("utf-8"))
        self.assertEqual(decoded["symbol"], symbol)
        # Verify raw bytes contain UTF-8 sequence for خودرو
        self.assertIn(symbol.encode("utf-8"), payload_bytes)

    def test_compile_order_payload_defaults(self):
        payload_bytes = compile_order_payload("وبملت", 200, 3000)
        data = json.loads(payload_bytes.decode("utf-8"))
        self.assertEqual(data["order_side"], "buy")
        self.assertTrue(data["is_max_price"])

    def test_compile_order_payload_custom_side_and_price(self):
        payload_bytes = compile_order_payload(
            symbol="فملی",
            qty=150,
            price=8000,
            order_side="sell",
            is_max_price=False,
        )
        data = json.loads(payload_bytes.decode("utf-8"))
        self.assertEqual(data["order_side"], "sell")
        self.assertFalse(data["is_max_price"])


class TestFireEpochCalculation(unittest.TestCase):
    def test_calculate_fire_epoch_zero_offset(self):
        target = 1700000000.0
        rtt = 0.040  # 40ms RTT
        # fire_epoch = target - 0.0 - (0.040 / 2) = 1700000000.0 - 0.020 = 1699999999.980
        fire = calculate_fire_epoch(target_epoch=target, ping_rtt_s=rtt, server_offset_s=0.0)
        self.assertAlmostEqual(fire, 1699999999.980, places=5)

    def test_calculate_fire_epoch_with_server_offset(self):
        target = 1700000000.0
        rtt = 0.050  # 50ms RTT -> half is 25ms
        offset = 0.010  # server is 10ms ahead
        # fire_epoch = 1700000000.0 - 0.010 - 0.025 = 1699999999.965
        fire = calculate_fire_epoch(target_epoch=target, ping_rtt_s=rtt, server_offset_s=offset)
        self.assertAlmostEqual(fire, 1699999999.965, places=5)

    def test_calculate_fire_epoch_negative_server_offset(self):
        target = 1700000000.0
        rtt = 0.020  # 20ms RTT -> half is 10ms
        offset = -0.015  # server is 15ms behind local clock
        # fire_epoch = 1700000000.0 - (-0.015) - 0.010 = 1700000000.005
        fire = calculate_fire_epoch(target_epoch=target, ping_rtt_s=rtt, server_offset_s=offset)
        self.assertAlmostEqual(fire, 1700000000.005, places=5)

    def test_calculate_fire_epoch_default_offset(self):
        target = 1000.0
        rtt = 0.100
        fire = calculate_fire_epoch(target, rtt)
        self.assertAlmostEqual(fire, 999.950, places=5)


class TestPreWarmScheduling(unittest.TestCase):
    def test_prewarm_window_is_strictly_60_seconds(self):
        # Strict user mandate: Pre-warming MUST start 60s (1 min) before target, not 5s
        self.assertEqual(PRE_WARM_WINDOW_SECONDS, 60.0)

    def test_calculate_prewarm_epoch(self):
        target = 1700000060.0
        prewarm_epoch = calculate_prewarm_epoch(target)
        self.assertAlmostEqual(target - prewarm_epoch, 60.0, places=5)
        self.assertAlmostEqual(prewarm_epoch, 1700000000.0, places=5)

    def test_engine_default_prewarm_lead(self):
        engine = DirectBlastEngine()
        self.assertEqual(engine.pre_warm_lead_s, 60.0)


class TestDirectBlastEngineAsync(unittest.IsolatedAsyncioTestCase):
    async def test_pre_warm_success(self):
        requested_urls = []

        def handler(request: httpx.Request):
            requested_urls.append(str(request.url))
            return httpx.Response(200, json={"status": "ok"})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        engine = DirectBlastEngine(client=client)

        success = await engine.pre_warm("https://m.easytrader.ir", timeout_s=60.0)
        self.assertTrue(success)
        self.assertTrue(engine.is_pre_warmed)
        self.assertGreaterEqual(len(requested_urls), 1)
        await engine.close()

    async def test_calibrate_ping(self):
        server_now = time.time() + 0.150  # 150ms server drift
        date_header_str = formatdate(server_now, usegmt=True)

        def handler(request: httpx.Request):
            return httpx.Response(200, headers={"Date": date_header_str}, json={"ping": "pong"})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        engine = DirectBlastEngine(client=client)

        rtt_s, offset_s = await engine.calibrate_ping("https://m.easytrader.ir")
        self.assertIsInstance(rtt_s, float)
        self.assertIsInstance(offset_s, float)
        self.assertGreaterEqual(rtt_s, 0.0)
        await engine.close()

    async def test_blast_concurrent_requests(self):
        received_requests = []

        def handler(request: httpx.Request):
            received_requests.append({
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "content": request.content,
            })
            return httpx.Response(200, json={"orderId": f"ORD-{len(received_requests)}", "status": "sent"})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        engine = DirectBlastEngine(client=client)

        headers = {
            "Authorization": "Bearer test-jwt-token",
            "Content-Type": "application/json",
        }
        payload = compile_order_payload("فولاد", 1000, 4500)

        results = await engine.blast(
            url="https://m.easytrader.ir/api/orders",
            payload_bytes=payload,
            headers=headers,
            count=3,
        )

        self.assertEqual(len(results), 3)
        self.assertEqual(len(received_requests), 3)
        for res in results:
            self.assertTrue(res["ok"])
            self.assertEqual(res["status_code"], 200)
            self.assertIn("ORD-", res["text"])

        for req in received_requests:
            self.assertEqual(req["method"], "POST")
            self.assertEqual(req["content"], payload)
            self.assertEqual(req["headers"]["authorization"], "Bearer test-jwt-token")

        await engine.close()

    async def test_blast_partial_error_handling(self):
        call_count = 0

        def handler(request: httpx.Request):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise httpx.ConnectTimeout("Connection timed out")
            return httpx.Response(200, json={"status": "ok"})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        engine = DirectBlastEngine(client=client)

        results = await engine.blast(
            url="https://m.easytrader.ir/api/orders",
            payload_bytes=b'{"test": 1}',
            headers={},
            count=3,
        )

        self.assertEqual(len(results), 3)
        ok_count = sum(1 for r in results if r["ok"])
        fail_count = sum(1 for r in results if not r["ok"])
        self.assertEqual(ok_count, 2)
        self.assertEqual(fail_count, 1)
        await engine.close()

    async def test_context_manager(self):
        def handler(request: httpx.Request):
            return httpx.Response(200, json={"status": "ok"})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        async with DirectBlastEngine(client=client) as engine:
            self.assertFalse(engine.is_closed)
            await engine.pre_warm("https://m.easytrader.ir")
            self.assertTrue(engine.is_pre_warmed)

        self.assertTrue(engine.is_closed)


class TestBlastUntilAccepted(unittest.IsolatedAsyncioTestCase):
    async def test_blast_until_accepted_reject_then_accept(self):
        call_count = 0

        def handler(request: httpx.Request):
            nonlocal call_count
            call_count += 1
            if call_count <= 5:
                return httpx.Response(200, json={"status": "rejected", "message": "خطا در هسته معاملات"})
            return httpx.Response(200, json={"order_id": 98765, "status": "accepted"})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        engine = DirectBlastEngine(client=client)

        result = await engine.blast_until_accepted(
            url="https://m.easytrader.ir/api/orders",
            payload_bytes=b'{"test":1}',
            headers={"Authorization": "Bearer token"},
            retry_interval_s=0.01,
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["attempts"], 6)
        self.assertIsNotNone(result["first_accepted_at"])
        self.assertEqual(result["last_status_code"], 200)
        self.assertIn("order_id", result["last_body_snippet"])
        await engine.close()

    async def test_blast_until_accepted_max_attempts_cap(self):
        def handler(request: httpx.Request):
            return httpx.Response(200, json={"status": "error", "message": "خطا: بازار بسته است"})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        engine = DirectBlastEngine(client=client)

        result = await engine.blast_until_accepted(
            url="https://m.easytrader.ir/api/orders",
            payload_bytes=b'{"test":1}',
            headers={},
            retry_interval_s=0.01,
            max_attempts=5,
        )

        self.assertFalse(result["accepted"])
        self.assertLessEqual(result["attempts"], 5)
        self.assertEqual(result["attempts"], 5)
        self.assertIsNone(result["first_accepted_at"])
        await engine.close()

    async def test_blast_until_accepted_429_backoff(self):
        call_count = 0

        def handler(request: httpx.Request):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return httpx.Response(429, json={"error": "Too Many Requests"})
            return httpx.Response(200, json={"order_id": 112233})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        engine = DirectBlastEngine(client=client)

        # Exponential backoff starts at 1.0s: attempt 1 (429) sleeps 1.0s, attempt 2 (429) sleeps 2.0s -> total >= 3.0s
        result = await engine.blast_until_accepted(
            url="https://m.easytrader.ir/api/orders",
            payload_bytes=b'{"test":1}',
            headers={},
            retry_interval_s=0.01,
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["attempts"], 3)
        self.assertGreaterEqual(result["elapsed_s"], 3.0)
        self.assertEqual(result["last_status_code"], 200)
        await engine.close()

    async def test_blast_until_accepted_attempt_log_jsonl(self):
        import tempfile
        call_count = 0

        def handler(request: httpx.Request):
            nonlocal call_count
            call_count += 1
            if call_count < 4:
                return httpx.Response(200, json={"message": "خطا - پذیرش ندارد"})
            return httpx.Response(200, json={"orderId": "ORD-LOG-1"})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        engine = DirectBlastEngine(client=client)

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "sub" / "attempt_log.jsonl"
            result = await engine.blast_until_accepted(
                url="https://m.easytrader.ir/api/orders",
                payload_bytes='{"stock":"فولاد"}'.encode("utf-8"),
                headers={},
                retry_interval_s=0.01,
                attempt_log_path=log_path,
            )

            self.assertTrue(result["accepted"])
            self.assertEqual(result["attempts"], 4)
            self.assertTrue(log_path.exists())

            with open(log_path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]

            self.assertEqual(len(lines), result["attempts"])
            entries = [json.loads(line) for line in lines]
            for idx, entry in enumerate(entries, start=1):
                self.assertEqual(entry["attempt"], idx)
                self.assertIn("ts", entry)
                self.assertIn("status_code", entry)
                self.assertIn("ok", entry)
                self.assertIn("body_snippet", entry)

            # Last entry should be accepted
            self.assertTrue(entries[-1]["ok"])
            self.assertFalse(entries[0]["ok"])

        await engine.close()

    async def test_blast_until_accepted_empty_markers_json_check(self):
        call_count = 0

        def handler(request: httpx.Request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(200, json={"err": "Invalid window"})
            return httpx.Response(200, json={"result": "created"})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        engine = DirectBlastEngine(client=client)

        result = await engine.blast_until_accepted(
            url="https://m.easytrader.ir/api/orders",
            payload_bytes=b'{"stock":"test"}',
            headers={},
            retry_interval_s=0.01,
            success_markers=[],
            error_markers=["خطا", "failed"],
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["attempts"], 2)
        await engine.close()


class TestDirectBlastAPIIntegration(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from main import app
        self.client = TestClient(app)

    @patch("main.DirectBlastEngine.blast")
    @patch("main.DirectBlastEngine.calibrate_ping")
    @patch("main.DirectBlastEngine.pre_warm")
    def test_direct_blast_endpoint_immediate(self, mock_prewarm, mock_calibrate, mock_blast):
        mock_prewarm.return_value = True
        mock_calibrate.return_value = (0.012, 0.001)
        mock_blast.return_value = [
            {"index": 0, "ok": True, "status_code": 200, "text": '{"orderId":"1"}', "headers": {}, "latency_ms": 10.5, "error": None},
            {"index": 1, "ok": True, "status_code": 200, "text": '{"orderId":"2"}', "headers": {}, "latency_ms": 11.2, "error": None},
            {"index": 2, "ok": True, "status_code": 200, "text": '{"orderId":"3"}', "headers": {}, "latency_ms": 10.8, "error": None},
        ]

        resp = self.client.post(
            "/api/easytrader/direct_blast",
            json={
                "stock": "فولاد",
                "qty": 500,
                "price_mode": "max",
                "burst_count": 3,
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["stock"], "فولاد")
        self.assertEqual(data["qty"], 500)
        self.assertTrue(data["prewarm_60s"])
        self.assertAlmostEqual(data["ping_rtt_ms"], 12.0, places=1)
        self.assertEqual(len(data["results"]), 3)
        mock_prewarm.assert_awaited_once()
        mock_calibrate.assert_awaited_once()
        mock_blast.assert_awaited_once()

    @patch("main.asyncio.sleep")
    @patch("main.DirectBlastEngine.blast")
    @patch("main.DirectBlastEngine.calibrate_ping")
    @patch("main.DirectBlastEngine.pre_warm")
    def test_direct_blast_endpoint_scheduled(self, mock_prewarm, mock_calibrate, mock_blast, mock_sleep):
        mock_prewarm.return_value = True
        mock_calibrate.return_value = (0.020, 0.000)
        mock_blast.return_value = [
            {"index": 0, "ok": True, "status_code": 200, "text": '{"orderId":"10"}', "headers": {}, "latency_ms": 8.0, "error": None}
        ]

        now = time.time()
        target = now + 120.0  # 120s in future -> pre-warm in 60s
        resp = self.client.post(
            "/api/easytrader/direct_blast",
            json={
                "stock": "شستا",
                "qty": 1000,
                "target_epoch": target,
                "burst_count": 1,
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["stock"], "شستا")
        self.assertEqual(data["qty"], 1000)
        self.assertTrue(data["prewarm_60s"])
        # Verify pre-warm was scheduled (mock_sleep called for pre-warm window and/or fire epoch)
        self.assertGreaterEqual(mock_sleep.await_count, 1)

    def test_direct_blast_endpoint_validations(self):
        # Missing stock
        resp = self.client.post("/api/easytrader/direct_blast", json={"stock": "", "qty": 10})
        self.assertEqual(resp.status_code, 400)

        # Zero qty
        resp = self.client.post("/api/easytrader/direct_blast", json={"stock": "فولاد", "qty": 0})
        self.assertEqual(resp.status_code, 400)

        # Invalid custom price
        resp = self.client.post(
            "/api/easytrader/direct_blast",
            json={"stock": "فولاد", "qty": 10, "price_mode": "custom", "price_value": "abc"},
        )
        self.assertEqual(resp.status_code, 400)

    @patch("main.DirectBlastEngine.blast")
    @patch("main.DirectBlastEngine.calibrate_ping")
    @patch("main.DirectBlastEngine.pre_warm")
    def test_easytrader_buy_routing_direct_blast(self, mock_prewarm, mock_calibrate, mock_blast):
        mock_prewarm.return_value = True
        mock_calibrate.return_value = (0.015, 0.0)
        mock_blast.return_value = [
            {"index": 0, "ok": True, "status_code": 200, "text": '{"ok":true}', "headers": {}, "latency_ms": 9.5, "error": None}
        ]

        resp = self.client.post(
            "/api/easytrader/buy",
            json={
                "username": "user123",
                "password": "pass123",
                "stock": "خودرو",
                "qty": 200,
                "direct_blast": True,
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["stock"], "خودرو")
        self.assertEqual(data["qty"], 200)
        self.assertTrue(data["prewarm_60s"])
        mock_prewarm.assert_awaited_once()
        mock_blast.assert_awaited_once()

    @patch("main.DirectBlastEngine.blast")
    @patch("main.DirectBlastEngine.calibrate_ping")
    @patch("main.DirectBlastEngine.pre_warm")
    def test_easytrader_buy_routing_direct_mode(self, mock_prewarm, mock_calibrate, mock_blast):
        mock_prewarm.return_value = True
        mock_calibrate.return_value = (0.015, 0.0)
        mock_blast.return_value = [
            {"index": 0, "ok": True, "status_code": 200, "text": '{"ok":true}', "headers": {}, "latency_ms": 9.5, "error": None}
        ]

        resp = self.client.post(
            "/api/easytrader/buy",
            json={
                "username": "user123",
                "password": "pass123",
                "stock": "وبملت",
                "qty": 300,
                "direct_mode": True,
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["stock"], "وبملت")
        self.assertEqual(data["qty"], 300)
        self.assertTrue(data["prewarm_60s"])
        mock_prewarm.assert_awaited_once()
        mock_blast.assert_awaited_once()

    @patch("main.DirectBlastEngine.pre_warm")
    @patch("main.DirectBlastEngine.calibrate_ping")
    def test_direct_blast_until_accepted_poll_flow(self, mock_calibrate, mock_prewarm):
        mock_prewarm.return_value = True
        mock_calibrate.return_value = (0.010, 0.0)

        async def fake_blast(*args, **kwargs):
            on_attempt = kwargs.get("on_attempt")
            if on_attempt:
                on_attempt({"attempt": 1, "status_code": 200, "body_snippet": "خطا", "accepted": False, "elapsed_s": 0.01})
                on_attempt({"attempt": 2, "status_code": 200, "body_snippet": '{"order_id": 999}', "accepted": True, "first_accepted_at": time.time(), "elapsed_s": 0.02})
            return {
                "accepted": True,
                "attempts": 2,
                "first_accepted_at": time.time(),
                "elapsed_s": 0.02,
                "last_status_code": 200,
                "last_body_snippet": '{"order_id": 999}',
            }

        with patch("main.DirectBlastEngine.blast_until_accepted", side_effect=fake_blast):
            resp = self.client.post(
                "/api/easytrader/direct_blast",
                json={
                    "stock": "فولاد",
                    "qty": 100,
                    "until_accepted": True,
                },
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIn("task_id", data)
            self.assertTrue(data["until_accepted"])
            task_id = data["task_id"]

            # Poll status until accepted
            status_data = None
            for _ in range(20):
                st_resp = self.client.get(f"/api/easytrader/direct_blast/status?task_id={task_id}")
                self.assertEqual(st_resp.status_code, 200)
                status_data = st_resp.json()
                if status_data["status"] == "accepted":
                    break
                time.sleep(0.05)

            self.assertEqual(status_data["status"], "accepted")
            self.assertEqual(status_data["attempts"], 2)
            self.assertIsNotNone(status_data["first_accepted_at"])
            self.assertEqual(status_data["last_status_code"], 200)

    @patch("main.DirectBlastEngine.pre_warm")
    @patch("main.DirectBlastEngine.calibrate_ping")
    def test_direct_blast_until_accepted_stop_flow(self, mock_calibrate, mock_prewarm):
        mock_prewarm.return_value = True
        mock_calibrate.return_value = (0.010, 0.0)

        async def fake_hanging_blast(*args, **kwargs):
            on_attempt = kwargs.get("on_attempt")
            if on_attempt:
                on_attempt({"attempt": 1, "status_code": 200, "body_snippet": "خطا", "accepted": False, "elapsed_s": 0.01})
            while True:
                await asyncio.sleep(0.05)

        with patch("main.DirectBlastEngine.blast_until_accepted", side_effect=fake_hanging_blast):
            resp = self.client.post(
                "/api/easytrader/direct_blast",
                json={
                    "stock": "فولاد",
                    "qty": 50,
                    "until_accepted": True,
                },
            )
            self.assertEqual(resp.status_code, 200)
            task_id = resp.json()["task_id"]

            # Wait briefly so task is running
            time.sleep(0.05)

            # Check status running
            st_resp = self.client.get(f"/api/easytrader/direct_blast/status?task_id={task_id}")
            self.assertEqual(st_resp.status_code, 200)
            self.assertIn(st_resp.json()["status"], ("running", "stopped"))

            # Call stop endpoint
            stop_resp = self.client.post(
                "/api/easytrader/direct_blast/stop",
                json={"task_id": task_id},
            )
            self.assertEqual(stop_resp.status_code, 200)
            self.assertTrue(stop_resp.json()["ok"])
            self.assertEqual(stop_resp.json()["status"], "stopped")

            # Check status is now stopped
            final_status = self.client.get(f"/api/easytrader/direct_blast/status?task_id={task_id}")
            self.assertEqual(final_status.status_code, 200)
            self.assertEqual(final_status.json()["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
