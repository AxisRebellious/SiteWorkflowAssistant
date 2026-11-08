# Direct Blast Engine & Simplified UI for EasyTrader

## 1. Overview
The current browser-based execution engine suffers from 300–800ms of DOM and rendering overhead, placing orders thousands of positions behind competitive direct API bots in stock queues.
This specification defines the Direct Blast architecture:
1. Direct API packet execution (bypassing DOM during the critical firing instant).
2. Autonomous pre-warming (TCP/TLS handshake 5s before target) and RTT ping compensation.
3. Simplified, zero-confusion Persian UI with persistent user credentials ("Remember Me") and removal of the Dry-Run checkbox.
4. Complete removal/bypassing of the license enforcement system.

## 2. Architecture & Components

### 2.1. Direct Blast Engine (`direct_blast.py`)
- **Token Capture:** Intercepts authenticated headers (`Authorization: Bearer <JWT>`, cookies, user-agent, CSRF) from the interactive session.
- **Pre-Compilation:** Encodes the exact order payload into raw UTF-8 bytes in memory prior to the firing trigger. Zero serialization during the critical milliseconds.
- **Pre-Warming:** At `target - 5.0s`, establishes an `httpx.AsyncClient(http2=True)` persistent connection to EasyTrader OMS endpoints with Keep-Alive to eliminate DNS/TCP/TLS handshake overhead.
- **Ping Calibration:** 30 seconds prior, issues lightweight RTT probes to measure round-trip time (`RTT`) and server time offset (`delta`).
  $$\text{Fire Epoch} = \text{Target Epoch} - \text{delta} - \left(\frac{\text{RTT}}{2.0}\right)$$
- **Multi-Socket Burst:** Fires asynchronous order requests in rapid concurrent bursts upon the trigger epoch.

### 2.2. License Gate Removal
- In `license.py` and `main.py`:
  - `verify_license()` returns valid/unlimited status unconditionally.
  - Remove license check barriers from `/api/easytrader/buy` and frontend UI.
  - License warning badges and modals are permanently deactivated.

### 2.3. Simplified UI (`static/easytrader.html` & `static/easytrader.js`)
- **Form Simplification:**
  - Remove "خرید آزمایشی (Dry Run)" checkbox completely.
  - Expose only essential fields: Username, Password, Symbol, Quantity, Price (Max/Custom), Schedule Time (`HH:MM:SS`).
  - Add "ذخیره اطلاعات من" (Save My Info) toggle to persist non-sensitive or user preferences in `localStorage` securely.
- **Auto-Execution Button:**
  - Single primary action: "شروع سرخطی خودکار" (Start Autonomous Front-Running).
  - Clean status indicator showing real-time countdown, ping latency, and pre-warm status.

## 3. Verification & Acceptance Criteria
- Unit tests for `direct_blast.py` payload compilation, ping calculation, and connection pre-warming.
- Offline mock verification verifying that direct socket transmission bypasses DOM execution.
- License-free execution verification across all endpoints without license files.
- UI inspection confirming removal of dry-run and presence of credentials persistence.
