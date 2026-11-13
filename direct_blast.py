"""
Direct Blast Engine for ultra-low latency order execution.

Features:
1. Pre-compilation of order payloads to raw UTF-8 bytes to achieve zero serialization
   latency during the critical firing instant (08:45:00.000).
2. Precise fire epoch calculation with RTT ping compensation and clock drift adjustment.
3. Strict 60-second connection pre-warming (Keep-Alive / HTTP/2 multiplexing) to eliminate
   DNS resolution and TLS handshake overhead.
4. Concurrent multi-request direct socket blasting via HTTP/2 connection pooling.
"""

from __future__ import annotations

import asyncio
from email.utils import parsedate_to_datetime
import json
import logging
from pathlib import Path
import time
from typing import Any

import httpx

logger = logging.getLogger("direct_blast")

# Strict Mandate: Pre-warming MUST start 60 seconds (1 minute) before the target epoch.
PRE_WARM_WINDOW_SECONDS: float = 60.0
PRE_WARM_LEAD_SECONDS: float = PRE_WARM_WINDOW_SECONDS

DEFAULT_ERROR_MARKERS: list[str] = [
    "خطا",
    "پذیرش ندارد",
    "ناموفق",
    "error",
    "failed",
    "rejected",
]
DEFAULT_SUCCESS_MARKERS: list[str] = [
    "accepted",
    "order_id",
    "orderId",
    "شماره سفارش",
    "موفق",
]


def compile_order_payload(
    symbol: str,
    qty: int | str,
    price: int | str,
    order_side: str = "buy",
    is_max_price: bool = True,
) -> bytes:
    """
    Pre-compiles the exact order JSON into UTF-8 bytes in memory prior to the firing epoch.
    Ensures zero string formatting, dict creation, or serialization happens during execution.
    """
    qty_int = int(str(qty).strip())
    price_str = str(price).strip()
    price_int = int(price_str) if price_str.isdigit() else 0
    side = str(order_side).strip().lower()

    payload = {
        "symbol": str(symbol).strip(),
        "quantity": qty_int,
        "price": price_int,
        "order_side": side,
        "orderSide": side,
        "is_max_price": bool(is_max_price),
        "isMaxPrice": bool(is_max_price),
    }

    # Separators without spaces for minimal byte footprint and fastest network transit
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def calculate_fire_epoch(
    target_epoch: float,
    ping_rtt_s: float,
    server_offset_s: float = 0.0,
) -> float:
    """
    Calculates the exact local timestamp to fire the network packet:
        fire_epoch = target_epoch - server_offset_s - (ping_rtt_s / 2.0)

    This ensures the packet hits the broker OMS network card at 08:45:00.000 rather than
    being rejected early or queueing behind competitors.
    """
    return float(target_epoch) - float(server_offset_s) - (float(ping_rtt_s) / 2.0)


def calculate_prewarm_epoch(
    target_epoch: float,
    prewarm_lead_s: float = PRE_WARM_WINDOW_SECONDS,
) -> float:
    """
    Calculates the timestamp when connection pre-warming must start.
    Strictly 60 seconds before target epoch.
    """
    return float(target_epoch) - float(prewarm_lead_s)


class DirectBlastEngine:
    """
    Ultra-low latency HTTP/2 order blast engine with connection pre-warming
    and RTT ping compensation.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        pre_warm_lead_s: float = PRE_WARM_WINDOW_SECONDS,
    ) -> None:
        self.pre_warm_lead_s: float = float(pre_warm_lead_s)
        self._external_client: bool = client is not None
        self._client: httpx.AsyncClient | None = client
        self.is_pre_warmed: bool = False
        self.is_closed: bool = False
        self.last_rtt_s: float = 0.0
        self.last_server_offset_s: float = 0.0

    def _create_default_client(self) -> httpx.AsyncClient:
        """Creates an HTTP/2 enabled persistent async client with optimized connection pooling."""
        limits = httpx.Limits(
            max_keepalive_connections=50,
            max_connections=100,
            keepalive_expiry=120.0,
        )
        timeout = httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=15.0)
        try:
            return httpx.AsyncClient(http2=True, limits=limits, timeout=timeout)
        except Exception as exc:
            logger.warning("HTTP/2 unavailable, falling back to HTTP/1.1: %s", exc)
            return httpx.AsyncClient(http2=False, limits=limits, timeout=timeout)

    async def get_client(self) -> httpx.AsyncClient:
        """Returns the active persistent HTTP client, creating it if needed."""
        if self._client is None or self.is_closed:
            self._client = self._create_default_client()
            self.is_closed = False
        return self._client

    async def pre_warm(self, base_url: str, timeout_s: float = 60.0) -> bool:
        """
        Starts 60s before target epoch. Sends keep-alive / lightweight ping requests
        to establish and maintain open TCP/TLS HTTP/2 connections with keep-alive,
        eliminating DNS, TCP handshake, and TLS negotiation latency at 08:45:00.
        """
        client = await self.get_client()
        url = base_url.rstrip("/")
        probe_urls = [url, f"{url}/", f"{url}/ping", f"{url}/health"]

        for probe_url in probe_urls:
            try:
                # Try HEAD first for minimal payload
                resp = await client.head(probe_url, timeout=min(timeout_s, 5.0))
                if resp.status_code < 500 or resp.status_code in (404, 405):
                    self.is_pre_warmed = True
                    logger.info("Pre-warm successful via HEAD %s (status %d)", probe_url, resp.status_code)
                    return True
            except httpx.RequestError:
                pass

            try:
                # Fallback to GET
                resp = await client.get(probe_url, timeout=min(timeout_s, 5.0))
                if resp.status_code < 500 or resp.status_code in (404, 405):
                    self.is_pre_warmed = True
                    logger.info("Pre-warm successful via GET %s (status %d)", probe_url, resp.status_code)
                    return True
            except httpx.RequestError as exc:
                logger.debug("Probe failed for %s: %s", probe_url, exc)
                continue

        logger.warning("Pre-warm failed to establish verified connection to %s", base_url)
        return False

    async def calibrate_ping(self, base_url: str) -> tuple[float, float]:
        """
        Measures round-trip time (RTT) and server clock offset delta.
        Returns: (ping_rtt_s, server_offset_s)
        """
        client = await self.get_client()
        url = base_url.rstrip("/")
        t0 = time.perf_counter()
        t_epoch_start = time.time()
        server_offset_s = 0.0

        try:
            resp = await client.get(url, timeout=5.0)
            t1 = time.perf_counter()
            rtt_s = max(0.0001, t1 - t0)

            # Check HTTP Date header for server atomic clock sync
            date_hdr = resp.headers.get("date") or resp.headers.get("Date")
            if date_hdr:
                try:
                    server_epoch = parsedate_to_datetime(date_hdr).timestamp()
                    # Mid-request estimate of local epoch
                    local_mid_epoch = t_epoch_start + (rtt_s / 2.0)
                    server_offset_s = server_epoch - local_mid_epoch
                except Exception as exc:
                    logger.debug("Failed parsing Date header for clock calibration: %s", exc)

            # Check if JSON payload contains server epoch
            try:
                data = resp.json()
                if isinstance(data, dict):
                    server_ts = data.get("server_time") or data.get("serverTime") or data.get("timestamp")
                    if isinstance(server_ts, (int, float)):
                        if server_ts > 1_000_000_000_000:  # millisecond epoch
                            server_ts = server_ts / 1000.0
                        local_mid_epoch = t_epoch_start + (rtt_s / 2.0)
                        server_offset_s = float(server_ts) - local_mid_epoch
            except Exception:
                pass

        except Exception as exc:
            t1 = time.perf_counter()
            rtt_s = max(0.001, t1 - t0)
            logger.warning("calibrate_ping encountered error, using measured RTT: %s", exc)

        self.last_rtt_s = rtt_s
        self.last_server_offset_s = server_offset_s
        logger.info("Calibrated ping: RTT=%.3fms, Server Offset=%.3fms", rtt_s * 1000, server_offset_s * 1000)
        return rtt_s, server_offset_s

    async def blast(
        self,
        url: str,
        payload_bytes: bytes,
        headers: dict[str, str],
        count: int = 3,
    ) -> list[dict[str, Any]]:
        """
        Fires concurrent HTTP/2 requests using the pre-warmed client.
        Zero formatting or serialization during invocation.
        """
        client = await self.get_client()

        async def _fire_single(idx: int) -> dict[str, Any]:
            t_start = time.perf_counter()
            try:
                resp = await client.post(url, content=payload_bytes, headers=headers)
                t_end = time.perf_counter()
                latency_ms = round((t_end - t_start) * 1000, 3)
                return {
                    "index": idx,
                    "ok": resp.is_success,
                    "status_code": resp.status_code,
                    "text": resp.text,
                    "headers": dict(resp.headers),
                    "latency_ms": latency_ms,
                    "error": None,
                }
            except Exception as exc:
                t_end = time.perf_counter()
                latency_ms = round((t_end - t_start) * 1000, 3)
                logger.error("Blast request %d failed: %s", idx, exc)
                return {
                    "index": idx,
                    "ok": False,
                    "status_code": 0,
                    "text": "",
                    "headers": {},
                    "latency_ms": latency_ms,
                    "error": str(exc),
                }

        tasks = [_fire_single(i) for i in range(max(1, count))]
        return await asyncio.gather(*tasks)

    async def blast_until_accepted(
        self,
        url: str,
        payload_bytes: bytes,
        headers: dict[str, str] | None = None,
        retry_interval_s: float = 0.15,
        max_attempts: int = 20000,
        max_duration_s: float = 1800.0,
        success_markers: list[str] | None = None,
        error_markers: list[str] | None = None,
        attempt_log_path: str | Path | None = None,
        on_attempt: Any = None,
        initial_backoff_s: float = 1.0,
    ) -> dict[str, Any]:
        """
        Executes a relentless pre-open blast retry loop using the pre-warmed client
        until an ACCEPTED response is received from the server, or caps are reached.
        """
        client = await self.get_client()
        return await blast_until_accepted(
            url=url,
            payload_bytes=payload_bytes,
            headers=headers,
            retry_interval_s=retry_interval_s,
            max_attempts=max_attempts,
            max_duration_s=max_duration_s,
            success_markers=success_markers,
            error_markers=error_markers,
            attempt_log_path=attempt_log_path,
            client=client,
            on_attempt=on_attempt,
            initial_backoff_s=initial_backoff_s,
        )

    async def close(self) -> None:
        """Closes the underlying HTTP client session if owned by this engine."""
        if self._client is not None and not self.is_closed:
            await self._client.aclose()
            self.is_closed = True
            self.is_pre_warmed = False

    async def __aenter__(self) -> DirectBlastEngine:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()


async def blast_until_accepted(
    url: str,
    payload_bytes: bytes,
    headers: dict[str, str] | None = None,
    retry_interval_s: float = 0.15,
    max_attempts: int = 20000,
    max_duration_s: float = 1800.0,
    success_markers: list[str] | None = None,
    error_markers: list[str] | None = None,
    attempt_log_path: str | Path | None = None,
    client: httpx.AsyncClient | None = None,
    on_attempt: Any = None,
    initial_backoff_s: float = 1.0,
) -> dict[str, Any]:
    """
    Relentless pre-open blast engine (شلیک سرکش تا پذیرش).
    Reuses ONE persistent connection across all attempts, hammering until first accepted response.
    """
    s_markers = list(DEFAULT_SUCCESS_MARKERS) if success_markers is None else success_markers
    e_markers = list(DEFAULT_ERROR_MARKERS) if error_markers is None else error_markers

    def _is_accepted(status_code: int, text: str) -> bool:
        if status_code != 200:
            return False
        text_lower = text.lower()
        if s_markers:
            for sm in s_markers:
                if sm.lower() in text_lower:
                    return True
            return False
        # Branch: markers were explicitly provided empty -> treat 200 as success ONLY if body parses as JSON without an 'error'-style field
        for em in e_markers:
            if em.lower() in text_lower:
                return False
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                for k, v in data.items():
                    k_lower = k.lower()
                    if "error" in k_lower or "err" in k_lower:
                        if v:
                            return False
            return True
        except Exception:
            return False

    created_client = False
    if client is None:
        limits = httpx.Limits(max_keepalive_connections=50, max_connections=100, keepalive_expiry=120.0)
        timeout = httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=15.0)
        client = httpx.AsyncClient(limits=limits, timeout=timeout)
        created_client = True

    t_start = time.perf_counter()
    attempts = 0
    consecutive_429 = 0
    accepted = False
    first_accepted_at: float | None = None
    last_status_code = 0
    last_body_snippet = ""

    log_file: Path | None = Path(attempt_log_path) if attempt_log_path else None
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        while attempts < max_attempts:
            elapsed = time.perf_counter() - t_start
            if elapsed >= max_duration_s:
                break

            attempts += 1
            status_code = 0
            snippet = ""
            is_ok = False

            try:
                resp = await client.post(url, content=payload_bytes, headers=headers)
                status_code = resp.status_code
                snippet = resp.text[:200]
                last_status_code = status_code
                last_body_snippet = snippet

                if _is_accepted(status_code, resp.text):
                    accepted = True
                    is_ok = True
                    first_accepted_at = time.time()
                else:
                    is_ok = False
            except Exception as exc:
                snippet = str(exc)[:200]
                last_status_code = 0
                last_body_snippet = snippet
                is_ok = False

            if log_file:
                entry = {
                    "ts": round(time.time(), 3),
                    "attempt": attempts,
                    "status_code": status_code,
                    "ok": is_ok,
                    "body_snippet": snippet,
                }
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")

            if on_attempt:
                try:
                    on_attempt({
                        "attempt": attempts,
                        "status_code": status_code,
                        "body_snippet": snippet,
                        "accepted": accepted,
                        "first_accepted_at": first_accepted_at,
                        "elapsed_s": round(time.perf_counter() - t_start, 3),
                    })
                except Exception:
                    pass

            if accepted:
                break

            if status_code == 429:
                consecutive_429 += 1
                backoff_s = initial_backoff_s * (2 ** (consecutive_429 - 1))
                rem = max_duration_s - (time.perf_counter() - t_start)
                if rem <= 0:
                    break
                await asyncio.sleep(min(backoff_s, rem))
            else:
                consecutive_429 = 0
                rem = max_duration_s - (time.perf_counter() - t_start)
                if rem <= 0:
                    break
                await asyncio.sleep(min(retry_interval_s, rem))
    finally:
        if created_client and client is not None:
            await client.aclose()

    total_elapsed = round(time.perf_counter() - t_start, 3)
    return {
        "accepted": accepted,
        "attempts": attempts,
        "first_accepted_at": first_accepted_at,
        "elapsed_s": total_elapsed,
        "last_status_code": last_status_code,
        "last_body_snippet": last_body_snippet,
    }
