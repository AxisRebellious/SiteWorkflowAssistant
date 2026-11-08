# Direct Blast Engine & Simplified UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an ultra-low latency direct HTTP/2 order blast engine for EasyTrader with 60-second connection pre-warming and ping-offset compensation, remove the license gate, and simplify the UI with credential persistence.

**Architecture:** Bypasses DOM and Playwright clicking during the critical execution instant. A headless/interactive session extracts the JWT Bearer token and cookies into memory; 60 seconds prior to target, `direct_blast.py` pre-warms HTTP/2 Keep-Alive connections; 30 seconds prior it measures RTT ping offset; at the target epoch minus latency offset, it blasts raw pre-compiled byte buffers directly over sockets.

**Tech Stack:** Python 3, `httpx` (HTTP/2, Keep-Alive, async sockets), Vanilla JS / Tailwind-style CSS, FastAPI / Uvicorn.

## Global Constraints
- Target pre-warm start: exactly 60.0s before target epoch.
- Zero string formatting or JSON serialization at the fire instant (pre-compiled to `bytes`).
- License check must return unconditional active status everywhere without requiring `data/license.json`.
- Dry-run UI checkbox and logic must be completely removed from user view.
- User credentials & defaults must support "ذخیره اطلاعات من" (Remember Me) in `localStorage`.
- All execution must be delegated to subagents per the subagent-only mandate.

---

### Task 1: Unconditional License Bypass

**Files:**
- Modify: `license.py:46-105`
- Modify: `main.py:182-245`
- Test: `test_license_flow.py`

**Interfaces:**
- Consumes: Nothing
- Produces: `license.check_active()` returning `(True, "لایسنس دائمی فعال است", 99999)` unconditionally.

- [ ] **Step 1: Write test for unconditional license bypass**
Add test in `test_license_flow.py` asserting that even with no license file or empty token, `check_active` and `_get_license_info` return `active=True`.

- [ ] **Step 2: Run test to verify it fails**
Run: `python test_license_flow.py`
Expected: FAIL

- [ ] **Step 3: Implement unconditional bypass**
Update `license.py` and `main.py` so license verification always succeeds without requiring files or valid signatures.

- [ ] **Step 4: Run test to verify it passes**
Run: `python test_license_flow.py`
Expected: PASS

- [ ] **Step 5: Commit**
`git commit -m "feat(license): make license unconditionally active and remove verification barriers"`

---

### Task 2: Direct Blast Core Engine (`direct_blast.py`) & Tests

**Files:**
- Create: `direct_blast.py`
- Create: `test_direct_blast.py`

**Interfaces:**
- Consumes: target epoch, stock code, quantity, price, bearer token, cookies.
- Produces:
  - `compile_order_payload(symbol, qty, price) -> bytes`
  - `calculate_fire_epoch(target_epoch, ping_rtt_s, server_offset_s) -> float`
  - `pre_warm_connection(client, base_url)` starting at `target - 60.0s`
  - `blast_orders(client, payload_bytes, n_burst=3)`

- [ ] **Step 1: Write failing unit tests in `test_direct_blast.py`**
Test pre-compilation of order bytes, fire epoch calculation with RTT compensation, and pre-warm timing logic (60s).

- [ ] **Step 2: Run test to verify it fails**
Run: `python test_direct_blast.py`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement `direct_blast.py`**
Implement payload compilation to raw bytes, async HTTP/2 connection pooling with Keep-Alive, 60s pre-warming background task, and ping calibration.

- [ ] **Step 4: Run test to verify it passes**
Run: `python test_direct_blast.py`
Expected: PASS

- [ ] **Step 5: Commit**
`git commit -m "feat(engine): add direct blast engine with 60s pre-warm and RTT compensation"`

---

### Task 3: Backend Integration in `main.py`

**Files:**
- Modify: `main.py`
- Test: `test_easytrader_composer.py`

**Interfaces:**
- Consumes: `direct_blast.py`
- Produces: `/api/easytrader/direct_blast` endpoint and session token capture hook.

- [ ] **Step 1: Write test for session token extraction and direct blast API**
- [ ] **Step 2: Run test to verify failure**
- [ ] **Step 3: Implement token interceptor and API endpoint in `main.py`**
- [ ] **Step 4: Run test to verify pass**
- [ ] **Step 5: Commit**
`git commit -m "feat(api): integrate direct blast endpoint and token capture"`

---

### Task 4: Simplified Persian UI & "Remember Me" (`easytrader.html` & `easytrader.js`)

**Files:**
- Modify: `static/easytrader.html`
- Modify: `static/easytrader.js`

**Interfaces:**
- Consumes: `/api/easytrader/buy` or `/api/easytrader/direct_blast`
- Produces: 4-field clean interface, removal of dry-run toggle, addition of "ذخیره اطلاعات من" checkbox.

- [ ] **Step 1: Update `easytrader.html`**
Remove `et_dry_run` checkbox. Add `et_remember_me` checkbox. Add status banner for 60s pre-warm.
- [ ] **Step 2: Update `easytrader.js`**
Persist non-sensitive form fields into `localStorage` when `et_remember_me` is checked. Remove dry-run warnings and confirm dialogs.
- [ ] **Step 3: Verify syntax and lint**
Run: `node --check static/easytrader.js`
- [ ] **Step 4: Commit**
`git commit -m "feat(ui): simplify EasyTrader UI, remove dry run, and add credential persistence"`

---

### Task 5: End-to-End Verification & Build Release

**Files:**
- All touched files
- Build: `SiteWorkflowAssistant_Licensed_v14.3_FINAL.zip` & `SiteWorkflowAssistant_Update_v14.3.zip`

- [ ] **Step 1: Run full test suite**
`python test_direct_blast.py && python test_license_flow.py && python test_easytrader_composer.py && python test_engine_upgrades.py`
- [ ] **Step 2: Rebuild distribution packages**
Run build script to package update and final zips.
- [ ] **Step 3: Commit and verify**
`git commit -m "release: v14.4 with direct blast engine and simplified UI"`
