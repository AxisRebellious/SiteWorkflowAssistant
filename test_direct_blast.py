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


if __name__ == "__main__":
    unittest.main()
