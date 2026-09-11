/**
 * EasyTrader (Mofid PWA) Dedicated Fast Buy Assistant
 * Frontend controller: Single Buy (Real / Dry-Run), Queue Execution, Live Log Polling
 */

(function () {
  "use strict";

  const STORAGE_KEY_QUEUE = "easytrader_orders_queue_v1";

  // State
  let activeCancelId = null;
  let isSingleRunning = false;
  let isQueueRunning = false;
  let logPollInterval = null;
  let isLicenseActive = true;
  let licenseHeartbeatInterval = null;

  // DOM Helpers
  function el(id) {
    return document.getElementById(id);
  }

  function showToast(message, isOk = true) {
    const t = el("toast");
    if (!t) return;
    const b = document.createElement("div");
    b.className = `banner ${isOk ? "ok" : "err"}`;
    b.textContent = message;
    t.appendChild(b);
    setTimeout(() => {
      b.remove();
    }, 5000);
  }

  function logAppend(text) {
    const box = el("et_log");
    if (!box) return;
    const time = new Date().toLocaleTimeString("fa-IR");
    box.textContent += `\n[${time}] ${text}`;
    box.scrollTop = box.scrollHeight;
  }

  function logSet(text) {
    const box = el("et_log");
    if (!box) return;
    box.textContent = text;
    box.scrollTop = box.scrollHeight;
  }

  // Log Polling
  function startLogPolling() {
    stopLogPolling();
    logPollInterval = setInterval(async () => {
      try {
        const res = await fetch("/api/easytrader/logs?lines=50");
        if (!res.ok) return;
        const data = await res.json();
        if (data.ok && Array.isArray(data.lines) && data.lines.length > 0) {
          const box = el("et_log");
          if (box) {
            box.textContent = data.lines.join("\n");
            box.scrollTop = box.scrollHeight;
          }
        }
      } catch (err) {
        // silent fail on network glitch during poll
      }
    }, 650);
  }

  function stopLogPolling() {
    if (logPollInterval) {
      clearInterval(logPollInterval);
      logPollInterval = null;
    }
  }

  // Queue Storage & Management
  function loadQueue() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY_QUEUE);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
      console.error("Failed to load queue from localStorage:", e);
      return [];
    }
  }

  function saveQueue(items) {
    try {
      localStorage.setItem(STORAGE_KEY_QUEUE, JSON.stringify(items));
    } catch (e) {
      console.error("Failed to save queue to localStorage:", e);
    }
  }

  function renderQueue() {
    const queue = loadQueue();
    const listEl = el("et_queue_list");
    const emptyEl = el("et_queue_empty");
    const badgeEl = el("queue_count_badge");

    if (!listEl) return;
    listEl.innerHTML = "";

    if (badgeEl) {
      badgeEl.textContent = `${queue.length} سفارش در صف`;
    }

    if (queue.length === 0) {
      if (emptyEl) emptyEl.style.display = "block";
      return;
    }

    if (emptyEl) emptyEl.style.display = "none";

    queue.forEach((item, index) => {
      const row = document.createElement("div");
      row.className = "queue-item";
      row.id = `qitem_${item.id}`;

      // Status pill text and class
      let statusHtml = '<span class="status-pill status-pending">⏳ در انتظار</span>';
      if (item.status === "running") {
        statusHtml = '<span class="status-pill status-running">⚡ در حال خرید...</span>';
      } else if (item.status === "success") {
        statusHtml = '<span class="status-pill status-success">✅ موفق</span>';
      } else if (item.status === "error") {
        statusHtml = `<span class="status-pill status-error" title="${item.error || 'خطا'}">❌ خطا</span>`;
      } else if (item.status === "cancelled") {
        statusHtml = '<span class="status-pill status-cancelled">⏹️ متوقف شد</span>';
      }

      row.innerHTML = `
        <span class="queue-idx">#${index + 1}</span>
        <div class="queue-title">
          <strong>${escapeHtml(item.stock)}</strong>
          <span style="color: var(--text-muted); font-size: 0.85rem; margin-right: 12px;">حجم: ${Number(item.qty).toLocaleString("fa-IR")} سهم</span>
          <span style="color: var(--text-muted); font-size: 0.85rem; margin-right: 8px;">(${priceLabel(item)})</span>
        </div>
        <div>${statusHtml}</div>
        <div class="queue-actions">
          <button type="button" class="danger small btn-del-item" data-id="${item.id}" ${isQueueRunning || isSingleRunning ? "disabled" : ""}>حذف</button>
        </div>
      `;

      listEl.appendChild(row);
    });

    // Attach delete listeners
    const delButtons = listEl.querySelectorAll(".btn-del-item");
    delButtons.forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const id = e.target.getAttribute("data-id");
        deleteQueueItem(id);
      });
    });
  }

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function addToQueue() {
    const stock = el("et_stock").value.trim();
    const qty = el("et_qty").value.trim();

    if (!stock) {
      showToast("لطفاً نام یا نماد سهم را وارد کنید.", false);
      el("et_stock").focus();
      return;
    }
    if (!qty || Number(qty) <= 0) {
      showToast("لطفاً تعداد معتبر وارد کنید.", false);
      el("et_qty").focus();
      return;
    }
    const qprice = getPriceSettings();
    if (qprice.mode === "custom" && !(Number(qprice.value) > 0)) {
      showToast("لطفاً قیمت دلخواه معتبر وارد کنید.", false);
      el("et_price").focus();
      return;
    }

    const queue = loadQueue();
    const newItem = {
      id: "q_" + Date.now() + "_" + Math.random().toString(36).substring(2, 7),
      stock: stock,
      qty: qty,
      price_mode: qprice.mode,
      price_value: qprice.value,
      status: "pending",
      error: null,
    };

    queue.push(newItem);
    saveQueue(queue);
    renderQueue();
    showToast(`نماد «${stock}» با تعداد ${qty} (${priceLabel(qprice)}) به صف خرید اضافه شد.`, true);
  }

  function deleteQueueItem(id) {
    if (isQueueRunning) {
      showToast("صف در حال اجراست و امکان حذف آیتم نیست.", false);
      return;
    }
    const queue = loadQueue();
    const filtered = queue.filter((item) => item.id !== id);
    saveQueue(filtered);
    renderQueue();
  }

  function clearQueue() {
    if (isQueueRunning) {
      showToast("صف در حال اجراست و امکان پاک‌سازی وجود ندارد.", false);
      return;
    }
    if (!confirm("آیا از پاک کردن تمامی سفارش‌های صف اطمینان دارید؟")) return;
    saveQueue([]);
    renderQueue();
    showToast("صف خرید با موفقیت خالی شد.", true);
  }

  // Get credentials
  function getCredentials() {
    const username = el("et_username").value.trim();
    const password = el("et_password").value.trim();
    return { username, password };
  }

  // License and Overlay Helpers
  function setBuyButtonsDisabled(disabled) {
    const buyBtn = el("btn_buy_single");
    const queueBtn = el("btn_queue_run");
    const addBtn = el("btn_add_to_queue");
    if (buyBtn) buyBtn.disabled = disabled;
    if (queueBtn) queueBtn.disabled = disabled;
    if (addBtn) addBtn.disabled = disabled;
  }

  function showLicenseOverlay(message) {
    let overlay = el("et_license_overlay");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "et_license_overlay";
      overlay.style.cssText = `
        position: fixed;
        inset: 0;
        background: rgba(5, 5, 8, 0.94);
        backdrop-filter: blur(14px);
        -webkit-backdrop-filter: blur(14px);
        z-index: 999999;
        display: flex;
        align-items: center;
        justify-content: center;
        direction: rtl;
        padding: 24px;
      `;
      document.body.appendChild(overlay);
    }

    overlay.innerHTML = `
      <div style="
        background: rgba(18, 18, 24, 0.98);
        border: 1px solid rgba(239, 68, 68, 0.45);
        border-radius: 16px;
        box-shadow: 0 10px 40px rgba(0,0,0,0.85), 0 0 30px rgba(239, 68, 68, 0.2);
        max-width: 480px;
        width: 100%;
        padding: 36px 28px;
        text-align: center;
        color: #f0f0f0;
        font-family: inherit;
      ">
        <div style="font-size: 3.5rem; margin-bottom: 12px; line-height: 1;">🔒</div>
        <h2 style="margin: 0 0 12px; font-size: 1.35rem; color: #fca5a5; font-weight: 700;">
          دسترسی مسدود است — لایسنس فعال نیست
        </h2>
        <p style="margin: 0 0 24px; color: #a1a1aa; font-size: 0.95rem; line-height: 1.7;">
          ${message || "اعتبار لایسنس شما به پایان رسیده یا لایسنس ثبت نشده است."}
        </p>
        <div>
          <a href="/" style="
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            background: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%);
            color: #ffffff;
            padding: 12px 24px;
            border-radius: 8px;
            font-weight: 600;
            text-decoration: none;
            box-shadow: 0 4px 15px rgba(59, 130, 246, 0.3);
            transition: all 0.2s;
          ">🔑 رفتن به صفحه فعال‌سازی لایسنس</a>
        </div>
      </div>
    `;
    overlay.style.display = "flex";
  }

  function hideLicenseOverlay() {
    const overlay = el("et_license_overlay");
    if (overlay) {
      overlay.style.display = "none";
    }
  }

  async function checkLicenseStatus() {
    try {
      const res = await fetch("/api/license/status");
      if (!res.ok) {
        isLicenseActive = false;
        showLicenseOverlay("خطا در بررسی وضعیت لایسنس از سرور.");
        setBuyButtonsDisabled(true);
        return false;
      }
      const data = await res.json();
      if (!data.active) {
        isLicenseActive = false;
        showLicenseOverlay(data.message || "لایسنس نرم‌افزار معتبر یا فعال نیست.");
        setBuyButtonsDisabled(true);
        return false;
      }

      // License is active
      isLicenseActive = true;
      hideLicenseOverlay();
      if (!isSingleRunning && !isQueueRunning) {
        setBuyButtonsDisabled(false);
      }
      return true;
    } catch (err) {
      isLicenseActive = false;
      showLicenseOverlay("ارتباط با سرور دستیار برقرار نشد.");
      setBuyButtonsDisabled(true);
      return false;
    }
  }

  // Set running state in UI
  function setRunningState(type, running) {
    if (type === "single") {
      isSingleRunning = running;
      el("btn_buy_single").disabled = running || !isLicenseActive;
      el("btn_stop_single").disabled = !running;
      el("btn_queue_run").disabled = running || isQueueRunning || !isLicenseActive;
      el("btn_add_to_queue").disabled = running || !isLicenseActive;
      el("btn_queue_clear").disabled = running;
      el("et_single_status").textContent = running ? "در حال اجرای خرید..." : "آماده";
      el("et_single_status").style.color = running ? "#60a5fa" : "var(--text-muted)";
    } else if (type === "queue") {
      isQueueRunning = running;
      el("btn_queue_run").disabled = running || !isLicenseActive;
      el("btn_queue_stop").disabled = !running;
      el("btn_buy_single").disabled = running || isSingleRunning || !isLicenseActive;
      el("btn_add_to_queue").disabled = running || !isLicenseActive;
      el("btn_queue_clear").disabled = running;
    }
    renderQueue();
  }

  // Price mode: max (default) or custom value
  function getPriceSettings() {
    const modeEl = document.querySelector('input[name="et_price_mode"]:checked');
    const mode = modeEl ? modeEl.value : "max";
    const val = (el("et_price") || {}).value || "";
    return mode === "custom" ? { mode: "custom", value: String(val).trim() } : { mode: "max", value: "" };
  }

  function priceLabel(p) {
    if (p && p.mode === "custom" && p.value) return "قیمت: " + Number(p.value).toLocaleString("fa-IR");
    return "سقف قیمت";
  }

  // Execute a single buy request
  async function executeBuyRequest(stock, qty, dryRun, cancelId, priceOpts) {
    const creds = getCredentials();
    const retryMaxRaw = parseInt((el("et_retry_max") || {}).value || "900", 10);
    const retryWaitRaw = parseInt((el("et_retry_wait") || {}).value || "45", 10);
    const price = priceOpts && priceOpts.mode === "custom" ? { mode: "custom", value: priceOpts.value || "" } : { mode: "max", value: "" };
    const payload = {
      username: creds.username,
      password: creds.password,
      stock: stock,
      qty: qty,
      price_mode: price.mode,
      price_value: price.value,
      dry_run: dryRun,
      turbo_mode: true,
      cancellation_id: cancelId,
      submit_max_attempts: Math.max(1, Math.min(isNaN(retryMaxRaw) ? 900 : retryMaxRaw, 2000)),
      submit_wait_s: Math.max(5, Math.min(isNaN(retryWaitRaw) ? 45 : retryWaitRaw, 300)),
    };

    const res = await fetch("/api/easytrader/buy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const data = await res.json();
    if (!res.ok) {
      if (res.status === 403) {
        isLicenseActive = false;
        showLicenseOverlay(data.detail || "دسترسی مسدود است: لایسنس فعال نیست.");
        setBuyButtonsDisabled(true);
      }
      throw new Error(data.detail || data.error || `خطای سرور (${res.status})`);
    }
    return data;
  }

  // Single Buy Click
  async function handleSingleBuy() {
    if (!isLicenseActive) {
      showLicenseOverlay("لایسنس فعال نیست. لطفاً ابتدا لایسنس را در صفحه اصلی فعال کنید.");
      setBuyButtonsDisabled(true);
      return;
    }
    const creds = getCredentials();
    if (!creds.username || !creds.password) {
      showToast("لطفاً ابتدا نام کاربری و رمز عبور ایزی‌تریدر را در کارت ۱ وارد کنید.", false);
      el("et_username").focus();
      return;
    }

    const stock = el("et_stock").value.trim();
    const qty = el("et_qty").value.trim();
    if (!stock) {
      showToast("لطفاً نام یا نماد سهم را وارد کنید.", false);
      el("et_stock").focus();
      return;
    }
    if (!qty || Number(qty) <= 0) {
      showToast("لطفاً تعداد/حجم خرید را معتبر وارد کنید.", false);
      el("et_qty").focus();
      return;
    }
    const price = getPriceSettings();
    if (price.mode === "custom" && !(Number(price.value) > 0)) {
      showToast("لطفاً قیمت دلخواه معتبر وارد کنید.", false);
      el("et_price").focus();
      return;
    }

    const dryRun = el("et_dry_run").checked;
    activeCancelId = "et_run_" + Date.now() + "_" + Math.random().toString(36).slice(2, 7);

    setRunningState("single", true);
    logSet(`▶️ شروع خرید برای نماد: ${stock} | حجم: ${qty} | حالت: ${dryRun ? "آزمایشی (Dry-Run)" : "خرید واقعی"}`);
    startLogPolling();

    try {
      const result = await executeBuyRequest(stock, qty, dryRun, activeCancelId, price);
      stopLogPolling();

      // Check results
      const resList = result.results || [];
      const hasError = resList.some((r) => r.ok === false);
      const isCancelled = Boolean(result.playback_cancelled);

      if (isCancelled) {
        showToast("اجرای خرید با دستور توقف لغو شد.", false);
        logAppend("⏹️ عملیات توسط کاربر متوقف شد.");
      } else if (hasError) {
        const firstErr = resList.find((r) => r.ok === false);
        const errMsg = firstErr ? firstErr.error || "خطای ناشناخته در مراحل سناریو" : "خطا در اجرا";
        showToast(`خطا در اجرای خرید: ${errMsg}`, false);
        logAppend(`❌ خطا: ${errMsg}`);
      } else {
        if (dryRun) {
          showToast(`خرید آزمایشی «${stock}» با موفقیت انجام شد و مرورگر باز ماند.`, true);
          logAppend(`✅ خرید آزمایشی نماد «${stock}» با موفقیت تا انتخاب سقف قیمت انجام شد.`);
        } else {
          showToast(`سفارش خرید نماد «${stock}» با موفقیت در ایزی‌تریدر ثبت شد!`, true);
          logAppend(`🎉 سفارش خرید قطعی نماد «${stock}» با موفقیت ثبت نهایی شد.`);
        }
      }

      // Display final step logs if available
      resList.forEach((r) => {
        if (Array.isArray(r.log) && r.log.length > 0) {
          logAppend("\n--- مراحل اجرا ---");
          r.log.forEach((stepLog) => logAppend(stepLog));
        }
      });
    } catch (err) {
      stopLogPolling();
      showToast(`خطای ارتباط با سرور: ${err.message}`, false);
      logAppend(`❌ خطای سرور: ${err.message}`);
    } finally {
      activeCancelId = null;
      setRunningState("single", false);
    }
  }

  // Single Stop Click
  async function handleSingleStop() {
    if (!activeCancelId) return;
    try {
      logAppend("⏹️ در حال ارسال سیگنال توقف به سرور...");
      await fetch("/api/easytrader/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cancellation_id: activeCancelId }),
      });
      showToast("دستور توقف به مرورگر مخابره شد.", true);
    } catch (err) {
      showToast("خطا در ارسال دستور توقف: " + err.message, false);
    }
  }

  // Queue Sequential Run
  async function handleQueueRun() {
    if (!isLicenseActive) {
      showLicenseOverlay("لایسنس فعال نیست. لطفاً ابتدا لایسنس را در صفحه اصلی فعال کنید.");
      setBuyButtonsDisabled(true);
      return;
    }
    const creds = getCredentials();
    if (!creds.username || !creds.password) {
      showToast("لطفاً نام کاربری و رمز عبور را در کارت ۱ وارد کنید.", false);
      el("et_username").focus();
      return;
    }

    const queue = loadQueue();
    if (queue.length === 0) {
      showToast("صف سفارش‌ها خالی است. ابتدا نمادها را به صف اضافه کنید.", false);
      return;
    }

    const dryRun = el("et_dry_run").checked;
    setRunningState("queue", true);
    logSet(`▶️ شروع اجرای صف شامل ${queue.length} سفارش | حالت: ${dryRun ? "آزمایشی" : "خرید واقعی"}`);

    for (let i = 0; i < queue.length; i++) {
      if (!isQueueRunning) {
        logAppend("⏹️ اجرای صف متوقف شد.");
        break;
      }

      const item = queue[i];
      item.status = "running";
      saveQueue(queue);
      renderQueue();

      activeCancelId = "et_q_" + item.id + "_" + Date.now();
      logAppend(`\n--- در حال اجرای سفارش [${i + 1}/${queue.length}]: ${item.stock} (${item.qty} سهم) ---`);
      startLogPolling();

      try {
        const result = await executeBuyRequest(item.stock, item.qty, dryRun, activeCancelId, { mode: item.price_mode, value: item.price_value });
        stopLogPolling();

        const resList = result.results || [];
        const hasError = resList.some((r) => r.ok === false);
        const isCancelled = Boolean(result.playback_cancelled);

        if (isCancelled) {
          item.status = "cancelled";
          item.error = "متوقف شد";
          logAppend(`⏹️ سفارش ${item.stock} متوقف شد.`);
          saveQueue(queue);
          renderQueue();
          break; // Stop queue on cancel
        } else if (hasError) {
          const firstErr = resList.find((r) => r.ok === false);
          item.status = "error";
          item.error = firstErr ? firstErr.error : "خطا در مراحل سناریو";
          logAppend(`❌ سفارش ${item.stock} با خطا مواجه شد: ${item.error}`);
        } else {
          item.status = "success";
          item.error = null;
          logAppend(`✅ سفارش ${item.stock} با موفقیت پایان یافت.`);
        }
      } catch (err) {
        stopLogPolling();
        item.status = "error";
        item.error = err.message;
        logAppend(`❌ خطای سرور برای ${item.stock}: ${err.message}`);
      }

      saveQueue(queue);
      renderQueue();

      // Pause between queue items
      if (i < queue.length - 1 && isQueueRunning) {
        logAppend("⏳ مکث ۲ ثانیه‌ای قبل از سفارش بعدی صف...");
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
    }

    stopLogPolling();
    activeCancelId = null;
    setRunningState("queue", false);
    showToast("اجرای صف نوبتی به پایان رسید.", true);
    logAppend("\n🏁 پایان کامل صف نوبتی ایزی‌تریدر.");
  }

  // Queue Stop
  async function handleQueueStop() {
    isQueueRunning = false;
    logAppend("⏹️ درخواست توقف صف ثبت شد.");
    if (activeCancelId) {
      try {
        await fetch("/api/easytrader/cancel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cancellation_id: activeCancelId }),
        });
      } catch (e) {
        // ignore
      }
    }
    setRunningState("queue", false);
  }

  // Initialization
  window.addEventListener("DOMContentLoaded", () => {
    // Buttons
    el("btn_buy_single")?.addEventListener("click", handleSingleBuy);
    el("btn_stop_single")?.addEventListener("click", handleSingleStop);
    el("btn_add_to_queue")?.addEventListener("click", addToQueue);
    el("btn_queue_run")?.addEventListener("click", handleQueueRun);
    el("btn_queue_stop")?.addEventListener("click", handleQueueStop);
    el("btn_queue_clear")?.addEventListener("click", clearQueue);
    el("btn_clear_log")?.addEventListener("click", () => {
      logSet("لاگ پاک شد. منتظر آغاز عملیات...");
    });

    // Enter key in stock / qty inputs
    el("et_qty")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        handleSingleBuy();
      }
    });

    // Price mode toggle
    document.querySelectorAll('input[name="et_price_mode"]').forEach((r) => {
      r.addEventListener("change", () => {
        const m = document.querySelector('input[name="et_price_mode"]:checked');
        if (el("et_price")) el("et_price").disabled = !m || m.value !== "custom";
      });
    });

    // Initial render of queue
    renderQueue();

    // License heartbeat check (initial check + every 60s)
    checkLicenseStatus();
    licenseHeartbeatInterval = setInterval(checkLicenseStatus, 60000);
  });
})();
