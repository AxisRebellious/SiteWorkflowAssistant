/**
 * EasyTrader (Mofid PWA) Dedicated Fast Buy Assistant
 * Frontend controller: Autonomous Front-Running (60s pre-warm), Queue Execution,
 * Live Log Polling, Remember Me Credentials Persistence, Persistent Browser Session
 */

(function () {
  "use strict";

  const STORAGE_KEY_QUEUE = "easytrader_orders_queue_v1";
  const STORAGE_KEY_CREDS = "easytrader_saved_credentials_v1";

  // State
  let activeCancelId = null;
  let singleCancelRequested = false;
  let isSingleRunning = false;
  let isQueueRunning = false;
  let logPollInterval = null;

  // Scheduler State
  let scheduleTimer = null;
  let isScheduledWaiting = false;
  let scheduledMode = null; // 'single' | 'queue'
  let scheduledScheduleInfo = null;

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

    const isBusy = isQueueRunning || isSingleRunning || isScheduledWaiting;

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
        statusHtml = `<span class="status-pill status-error" title="${escapeHtml(item.error || 'خطا')}">❌ خطا</span>`;
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
          <button type="button" class="danger small btn-del-item" data-id="${item.id}" ${isBusy ? "disabled" : ""}>حذف</button>
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
    if (isScheduledWaiting || isQueueRunning || isSingleRunning) {
      showToast("در حین اجرای برنامه امکان افزودن به صف وجود ندارد.", false);
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
    if (isQueueRunning || isScheduledWaiting) {
      showToast("صف در حال اجرا یا زمان‌بندی است و امکان حذف آیتم نیست.", false);
      return;
    }
    const queue = loadQueue();
    const filtered = queue.filter((item) => item.id !== id);
    saveQueue(filtered);
    renderQueue();
  }

  function clearQueue() {
    if (isQueueRunning || isScheduledWaiting) {
      showToast("صف در حال اجرا یا زمان‌بندی است و امکان پاک‌سازی وجود ندارد.", false);
      return;
    }
    if (!confirm("آیا از پاک کردن تمامی سفارش‌های صف اطمینان دارید؟")) return;
    saveQueue([]);
    renderQueue();
    showToast("صف خرید با موفقیت خالی شد.", true);
  }

  // Get credentials
  function getCredentials() {
    const username = (el("et_username")?.value || "").trim();
    const password = (el("et_password")?.value || "").trim();
    return { username, password };
  }

  // Credentials Persistence ("ذخیره اطلاعات من")
  function saveCredentialsIfRemembered() {
    const rememberEl = el("et_remember_me");
    if (!rememberEl) return;
    if (!rememberEl.checked) {
      try {
        localStorage.removeItem(STORAGE_KEY_CREDS);
      } catch (e) {
        // silent
      }
      return;
    }

    const creds = getCredentials();
    const stock = (el("et_stock")?.value || "").trim();
    const qty = (el("et_qty")?.value || "").trim();
    const priceModeEl = document.querySelector('input[name="et_price_mode"]:checked');
    const price_mode = priceModeEl ? priceModeEl.value : "max";
    const price_value = (el("et_price")?.value || "").trim();
    const schedule_time = (el("et_schedule_time")?.value || "08:45:00").trim();

    const dataToSave = {
      username: creds.username,
      password: creds.password,
      stock,
      qty,
      price_mode,
      price_value,
      schedule_time,
    };

    try {
      localStorage.setItem(STORAGE_KEY_CREDS, JSON.stringify(dataToSave));
    } catch (e) {
      console.warn("Failed to persist credentials in localStorage:", e);
    }
  }

  function loadSavedCredentials() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY_CREDS);
      if (!raw) return;
      const data = JSON.parse(raw);
      if (!data || typeof data !== "object") return;

      if (data.username !== undefined && el("et_username")) {
        el("et_username").value = data.username;
      }
      if (data.password !== undefined && el("et_password")) {
        el("et_password").value = data.password;
      }
      if (data.stock !== undefined && el("et_stock")) {
        el("et_stock").value = data.stock;
      }
      if (data.qty !== undefined && el("et_qty")) {
        el("et_qty").value = data.qty;
      }
      if (data.price_mode) {
        const modeRadio = document.querySelector(`input[name="et_price_mode"][value="${data.price_mode}"]`);
        if (modeRadio) {
          modeRadio.checked = true;
        }
        if (el("et_price")) {
          el("et_price").disabled = data.price_mode !== "custom";
        }
      }
      if (data.price_value !== undefined && el("et_price")) {
        el("et_price").value = data.price_value;
      }
      if (data.schedule_time !== undefined && el("et_schedule_time")) {
        el("et_schedule_time").value = data.schedule_time;
      }
      if (el("et_remember_me")) {
        el("et_remember_me").checked = true;
      }
    } catch (e) {
      console.error("Failed to restore saved credentials:", e);
    }
  }

  // Unified Button & Status States
  function updateButtonsState() {
    const anyRunning = isSingleRunning || isQueueRunning;
    const isWaiting = isScheduledWaiting;

    if (el("btn_buy_single")) {
      el("btn_buy_single").disabled = anyRunning || isWaiting;
    }
    if (el("btn_queue_run")) {
      el("btn_queue_run").disabled = anyRunning || isWaiting;
    }
    if (el("btn_add_to_queue")) {
      el("btn_add_to_queue").disabled = anyRunning || isWaiting;
    }
    if (el("btn_queue_clear")) {
      el("btn_queue_clear").disabled = anyRunning || isWaiting;
    }
    if (el("btn_close_browser")) {
      el("btn_close_browser").disabled = anyRunning || isWaiting;
    }

    if (el("btn_stop_single")) {
      el("btn_stop_single").disabled = !(isSingleRunning || (isWaiting && scheduledMode === "single"));
    }
    if (el("btn_queue_stop")) {
      el("btn_queue_stop").disabled = !(isQueueRunning || (isWaiting && scheduledMode === "queue"));
    }

    if (isWaiting) {
      if (el("et_single_status")) {
        el("et_single_status").textContent = "زمان‌بندی فعال — آماده‌سازی از ۱ دقیقه قبل";
        el("et_single_status").style.color = "#fbbf24";
      }
    } else if (isSingleRunning) {
      if (el("et_single_status")) {
        el("et_single_status").textContent = "در حال اجرای سرخطی...";
        el("et_single_status").style.color = "#60a5fa";
      }
    } else {
      if (el("et_single_status")) {
        el("et_single_status").textContent = "آماده";
        el("et_single_status").style.color = "#4ade80";
      }
    }

    renderQueue();
  }

  function setRunningState(type, running) {
    if (type === "single") {
      isSingleRunning = running;
    } else if (type === "queue") {
      isQueueRunning = running;
    }
    updateButtonsState();
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

  // Schedule Helpers
  function formatCountdown(ms) {
    if (ms <= 0) return "00:00:00";
    const totalSec = Math.floor(ms / 1000);
    const s = totalSec % 60;
    const m = Math.floor(totalSec / 60) % 60;
    const h = Math.floor(totalSec / 3600);
    const pad = (n) => String(n).padStart(2, "0");
    if (h >= 24) {
      const days = Math.floor(h / 24);
      const remH = h % 24;
      return `${days} روز و ${pad(remH)}:${pad(m)}:${pad(s)}`;
    }
    return `${pad(h)}:${pad(m)}:${pad(s)}`;
  }

  function updateScheduleBanner(targetTimeStr, wakeTimeStr, remainingMs) {
    const banner = el("et_schedule_banner");
    if (!banner) return;
    const countdownStr = formatCountdown(remainingMs);
    banner.innerHTML = `
      <div style="display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px;">
        <div style="display: flex; align-items: center; flex-wrap: wrap; gap: 6px;">
          <span>⏳ <strong>سرخطی زمان‌بندی‌شده:</strong></span>
          <span>هدف: [<strong>${escapeHtml(targetTimeStr)}</strong>]</span>
          <span style="opacity: 0.6;">|</span>
          <span>گرم‌کردن کانکشن در: [<strong>${escapeHtml(wakeTimeStr)}</strong>]</span>
          <span style="opacity: 0.6;">|</span>
          <span>مانده تا گرم‌کردن: <span class="schedule-countdown">${countdownStr}</span></span>
          <span style="opacity: 0.6;">|</span>
          <span class="badge badge-turbo" style="margin-right: 4px;">سرخطی پرسرعت</span>
        </div>
        <button type="button" class="danger small" id="btn_banner_cancel" style="padding: 4px 12px; font-size: 0.82rem;">⏹️ لغو زمان‌بندی</button>
      </div>
    `;
    banner.classList.add("active");
    const cancelBtn = el("btn_banner_cancel");
    if (cancelBtn && !cancelBtn.dataset.bound) {
      cancelBtn.dataset.bound = "1";
      cancelBtn.addEventListener("click", () => cancelSchedule("با دکمه لغو در اعلان زمان‌بندی"));
    }
  }

  function hideScheduleBanner() {
    const banner = el("et_schedule_banner");
    if (banner) {
      banner.classList.remove("active");
      banner.innerHTML = "";
    }
  }

  function cancelSchedule(reason = "توسط کاربر") {
    singleCancelRequested = true;
    if (scheduleTimer) {
      clearInterval(scheduleTimer);
      scheduleTimer = null;
    }
    isScheduledWaiting = false;
    scheduledMode = null;
    scheduledScheduleInfo = null;
    hideScheduleBanner();
    logAppend(`⏹️ زمان‌بندی خودکار ${reason} لغو شد.`);
    showToast("زمان‌بندی خودکار لغو شد.", false);
    setRunningState("single", false);
    setRunningState("queue", false);
  }

  function initScheduleInputs() {
    const dateInput = el("et_schedule_date");
    const timeInput = el("et_schedule_time");

    if (dateInput && !dateInput.value) {
      const now = new Date();
      // If after 08:45:00 today, default to tomorrow's date
      const targetToday = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 8, 45, 0);
      const effectiveDate = (now.getTime() > targetToday.getTime())
        ? new Date(now.getTime() + 24 * 3600 * 1000)
        : now;

      const y = effectiveDate.getFullYear();
      const m = String(effectiveDate.getMonth() + 1).padStart(2, "0");
      const d = String(effectiveDate.getDate()).padStart(2, "0");
      dateInput.value = `${y}-${m}-${d}`;
    }

    if (timeInput && !timeInput.value) {
      timeInput.value = "08:45:00";
    }
  }

  function getScheduleSettings() {
    const enabledCheck = el("et_schedule_enabled");
    if (!enabledCheck || !enabledCheck.checked) {
      return { enabled: false };
    }

    const dateVal = (el("et_schedule_date")?.value || "").trim();
    const timeVal = (el("et_schedule_time")?.value || "").trim();

    if (!dateVal) {
      showToast("لطفاً تاریخ زمان‌بندی را مشخص کنید.", false);
      el("et_schedule_date")?.focus();
      return null;
    }
    if (!timeVal) {
      showToast("لطفاً ساعت دقیق زمان‌بندی را مشخص کنید.", false);
      el("et_schedule_time")?.focus();
      return null;
    }

    const dateParts = dateVal.split("-").map(Number);
    if (dateParts.length !== 3 || dateParts.some(isNaN)) {
      showToast("فرمت تاریخ نامعتبر است.", false);
      return null;
    }

    const timeParts = timeVal.split(":").map(Number);
    if (timeParts.length < 2 || timeParts.some(isNaN)) {
      showToast("فرمت ساعت نامعتبر است.", false);
      return null;
    }

    const [year, month, day] = dateParts;
    const [hours, minutes, seconds = 0] = timeParts;

    const targetDate = new Date(year, month - 1, day, hours, minutes, seconds || 0);
    const T_target = targetDate.getTime();
    if (isNaN(T_target)) {
      showToast("تاریخ یا زمان وارد شده نامعتبر است.", false);
      return null;
    }

    const now = Date.now();
    if (T_target < now) {
      showToast(`زمان هدف (${timeVal}) سپری شده است. لطفاً ساعت یا تاریخ آینده را تعیین کنید.`, false);
      el("et_schedule_time")?.focus();
      return null;
    }

    // Wake-up time: 60 seconds (1 minute) before T_target for pre-warm
    const T_wake = T_target - 60 * 1000;

    return {
      enabled: true,
      T_target,
      T_wake,
      dateStr: dateVal,
      timeStr: timeVal,
      targetDate,
      target_epoch: Math.floor(T_target / 1000),
      target_time: timeVal,
    };
  }

  function startScheduleCountdown(sched, mode) {
    if (scheduleTimer) {
      clearInterval(scheduleTimer);
      scheduleTimer = null;
    }

    isScheduledWaiting = true;
    scheduledMode = mode;
    scheduledScheduleInfo = sched;
    updateButtonsState();

    const targetTimeStr = sched.timeStr;
    const wakeDate = new Date(sched.T_wake);
    const wakeTimeStr = wakeDate.toLocaleTimeString("fa-IR", { hour12: false });

    logAppend(`⏱️ زمان‌بندی هوشمند شروع خودکار فعال شد.`);
    logAppend(`🎯 زمان هدف سرخطی: ${targetTimeStr}`);
    logAppend(`🚀 زمان گرم‌کردن کانکشن (۱ دقیقه قبل): ${wakeTimeStr}`);
    logAppend("✅ آماده‌سازی سرخطی فوق‌سریع: ارسال مستقیم سفارش در رأس ثانیه هدف.");
    logAppend(`⏳ سیستم تا زمان گرم‌کردن کانکشن در حالت شمارش معکوس قرار دارد...`);
    showToast(`زمان‌بندی فعال شد. گرم‌کردن کانکشن در ${wakeTimeStr}`, true);

    const updateTick = () => {
      const now = Date.now();
      const remainingMs = sched.T_wake - now;

      if (remainingMs <= 0) {
        // Time to wake up!
        clearInterval(scheduleTimer);
        scheduleTimer = null;
        isScheduledWaiting = false;
        scheduledMode = null;
        scheduledScheduleInfo = null;
        updateButtonsState();

        const banner = el("et_schedule_banner");
        if (banner) {
          banner.innerHTML = `<div>⚡ <strong>زمان آماده‌سازی و گرم‌کردن کانکشن فرا رسید!</strong> در حال آماده‌سازی و شلیک سرخطی (هدف: ${escapeHtml(targetTimeStr)})...</div>`;
        }

        logAppend(`\n🔔 زمان گرم‌کردن کانکشن فرا رسید! شروع خودکار اتوماسیون (۱ دقیقه پیش از سرخطی ${targetTimeStr})...`);
        showToast("گرم‌کردن کانکشن آغاز شد. آماده‌سازی برای شلیک سرخطی...", true);

        const execInfo = {
          target_epoch: sched.target_epoch,
          target_time: sched.target_time,
        };

        if (mode === "single") {
          runSingleBuyExecution(execInfo);
        } else if (mode === "queue") {
          runQueueExecution(execInfo);
        }
      } else {
        updateScheduleBanner(targetTimeStr, wakeTimeStr, remainingMs);
      }
    };

    updateTick();
    if (isScheduledWaiting) {
      scheduleTimer = setInterval(updateTick, 1000);
    }
  }

  // Execute a single buy request (with persistent browser & optional target epoch)
  async function executeBuyRequest(stock, qty, cancelId, priceOpts, extraParams = {}) {
    const creds = getCredentials();
    const retryMaxRaw = parseInt((el("et_retry_max") || {}).value || "900", 10);
    const retryWaitRaw = parseFloat((el("et_retry_wait") || {}).value || "0.002");
    const price = priceOpts && priceOpts.mode === "custom" ? { mode: "custom", value: priceOpts.value || "" } : { mode: "max", value: "" };

    // Persistent browser across operations
    const keepBrowser = extraParams.keep_browser !== undefined ? Boolean(extraParams.keep_browser) : true;

    const payload = {
      username: creds.username,
      password: creds.password,
      stock: stock,
      qty: qty,
      price_mode: price.mode,
      price_value: price.value,
      dry_run: false,
      turbo_mode: true,
      cancellation_id: cancelId,
      submit_max_attempts: Math.max(1, Math.min(isNaN(retryMaxRaw) ? 900 : retryMaxRaw, 1000000)),
      submit_wait_s: Math.max(0.001, Math.min(isNaN(retryWaitRaw) ? 0.002 : retryWaitRaw, 300)),
      keep_browser: keepBrowser,
    };

    if (extraParams.target_epoch) {
      payload.target_epoch = extraParams.target_epoch;
    }
    if (extraParams.target_time) {
      payload.target_time = extraParams.target_time;
    }

    const res = await fetch("/api/easytrader/buy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || data.error || `خطای سرور (${res.status})`);
    }
    return data;
  }

  // Single Buy Execution Runner
  async function runSingleBuyExecution(scheduleInfo = null) {
    const stock = el("et_stock").value.trim();
    const qty = el("et_qty").value.trim();
    const price = getPriceSettings();
    const repeat = Math.max(1, Math.min(parseInt(el("et_qty_repeat")?.value || "1", 10) || 1, 100));

    activeCancelId = "et_run_" + Date.now() + "_" + Math.random().toString(36).slice(2, 7);
    singleCancelRequested = false;

    setRunningState("single", true);
    const schedText = scheduleInfo ? ` | زمان‌بندی سرخطی: ${scheduleInfo.target_time}` : "";
    const repeatText = repeat > 1 ? ` | تکرار: ${repeat} بار` : "";
    logSet(`▶️ شروع سرخطی خودکار برای نماد: ${stock} | حجم: ${qty}${repeatText} | حالت: خرید واقعی فوق‌سریع${schedText}`);
    startLogPolling();

    let completedOrders = 0;
    let wasCancelled = false;
    let fatalError = null;
    let aggregatedResults = [];

    try {
      for (let i = 1; i <= repeat; i++) {
        if (singleCancelRequested) {
          wasCancelled = true;
          logAppend(`⏹️ عملیات پیش از سفارش ${i} از ${repeat} متوقف شد.`);
          break;
        }

        if (repeat > 1) {
          logAppend(`\n🔄 در حال ثبت سفارش ${i} از ${repeat} (نماد: ${stock} | حجم: ${qty})...`);
        }

        const extraParams = {
          keep_browser: true,
          target_epoch: (i === 1 && scheduleInfo) ? scheduleInfo.target_epoch : undefined,
          target_time: (i === 1 && scheduleInfo) ? scheduleInfo.target_time : undefined,
        };

        let result;
        try {
          result = await executeBuyRequest(stock, qty, activeCancelId, price, extraParams);
        } catch (reqErr) {
          if (singleCancelRequested || (reqErr.message && /cancel|لغو|توقف/i.test(reqErr.message))) {
            wasCancelled = true;
            logAppend(`⏹️ سفارش ${i} از ${repeat} با دستور توقف لغو شد.`);
          } else {
            fatalError = reqErr.message || "خطای ارتباط با سرور";
            logAppend(`❌ خطا در سفارش ${i} از ${repeat}: ${fatalError}`);
          }
          break;
        }

        const resList = result?.results || [];
        const hasError = resList.some((r) => r.ok === false);
        const isCancelled = Boolean(result?.playback_cancelled);

        resList.forEach((r) => aggregatedResults.push(r));

        if (isCancelled || singleCancelRequested) {
          wasCancelled = true;
          logAppend(`⏹️ عملیات در سفارش ${i} از ${repeat} توسط کاربر متوقف شد.`);
          break;
        }

        if (hasError) {
          const firstErr = resList.find((r) => r.ok === false);
          fatalError = firstErr ? firstErr.error || "خطای ناشناخته در مراحل سناریو" : "خطا در اجرا";
          logAppend(`❌ خطا در مرحله سفارش ${i} از ${repeat}: ${fatalError}`);
          break;
        }

        completedOrders++;
        if (repeat > 1) {
          logAppend(`✔️ سفارش ${i} از ${repeat} با موفقیت ثبت شد.`);
        }
      }

      stopLogPolling();

      // Check results
      if (wasCancelled) {
        if (repeat > 1) {
          showToast(`اجرای خرید با دستور توقف لغو شد (${completedOrders} از ${repeat} سفارش انجام شد).`, false);
        } else {
          showToast("اجرای خرید با دستور توقف لغو شد.", false);
        }
        logAppend("⏹️ عملیات توسط کاربر متوقف شد.");
      } else if (fatalError) {
        if (repeat > 1) {
          showToast(`خطا در سفارش ${completedOrders + 1} از ${repeat}: ${fatalError}`, false);
        } else {
          showToast(`خطا در اجرای خرید: ${fatalError}`, false);
        }
        logAppend(`❌ خطا: ${fatalError}`);
      } else {
        if (repeat > 1) {
          showToast(`هر ${repeat} سفارش خرید نماد «${stock}» با موفقیت در ایزی‌تریدر ثبت شد!`, true);
          logAppend(`🎉 تمام ${repeat} سفارش خرید قطعی نماد «${stock}» با موفقیت ثبت نهایی شد.`);
        } else {
          showToast(`سفارش خرید نماد «${stock}» با موفقیت در ایزی‌تریدر ثبت شد!`, true);
          logAppend(`🎉 سفارش خرید قطعی نماد «${stock}» با موفقیت ثبت نهایی شد.`);
        }
      }

      // Display final step logs if available
      aggregatedResults.forEach((r) => {
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
      singleCancelRequested = false;
      hideScheduleBanner();
      setRunningState("single", false);
    }
  }

  // Single Buy Click Handler
  async function handleSingleBuy() {
    // Save credentials if Remember Me is checked
    saveCredentialsIfRemembered();

    const creds = getCredentials();
    if (!creds.username || !creds.password) {
      showToast("لطفاً ابتدا نام کاربری و رمز عبور ایزی‌تریدر را در بخش ۱ وارد کنید.", false);
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

    // Check Schedule
    const sched = getScheduleSettings();
    if (sched === null) {
      // Validation error in schedule fields
      return;
    }

    if (sched.enabled) {
      const now = Date.now();
      if (now < sched.T_wake) {
        // Case 1: Waiting before the 60s pre-warm window
        startScheduleCountdown(sched, "single");
        return;
      } else if (now >= sched.T_wake && now < sched.T_target) {
        // Case 2: Inside the 60s prep window -> run immediately with target_epoch
        logAppend(`⚡ در بازه آماده‌سازی و گرم‌کردن کانکشن (کمتر از ۱ دقیقه تا هدف) قرار داریم. شروع فوری اتوماسیون (هدف سرخطی: ${sched.timeStr})...`);
        showToast(`در بازه آماده‌سازی ۶۰ ثانیه پیش از ${sched.timeStr} قرار داریم. شروع گرم‌کردن و شلیک سرخطی...`, true);
        const banner = el("et_schedule_banner");
        if (banner) {
          banner.innerHTML = `<div>⚡ <strong>گرم‌کردن کانکشن و آماده‌سازی:</strong> ارسال در رأس ساعت [<strong>${escapeHtml(sched.timeStr)}</strong>]</div>`;
          banner.classList.add("active");
        }
        await runSingleBuyExecution({ target_epoch: sched.target_epoch, target_time: sched.target_time });
        return;
      } else {
        // Case 3: now >= T_target -> execute immediately
        logAppend(`⚡ زمان هدف سپری شده یا هم‌اکنون است. اجرای فوری...`);
        await runSingleBuyExecution(null);
        return;
      }
    }

    // Direct execution without schedule
    await runSingleBuyExecution(null);
  }

  // Single Stop Click
  async function handleSingleStop() {
    singleCancelRequested = true;
    if (isScheduledWaiting && scheduledMode === "single") {
      cancelSchedule("با دکمه توقف خرید");
      return;
    }
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

  // Queue Execution Runner (Persistent Browser)
  async function runQueueExecution(scheduleInfo = null) {
    const queue = loadQueue();
    if (queue.length === 0) return;

    setRunningState("queue", true);

    const schedText = scheduleInfo ? ` | زمان‌بندی هدف: ${scheduleInfo.target_time}` : "";
    logSet(`▶️ شروع اجرای صف شامل ${queue.length} سفارش | حالت: خرید واقعی${schedText}`);

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

      if (i === 0) {
        logAppend(`\n--- در حال اجرای سفارش [${i + 1}/${queue.length}]: ${item.stock} (${item.qty} سهم) ---`);
        logAppend(`🚀 باز کردن مرورگر و ورود به ایزی‌تریدر...`);
      } else {
        logAppend(`\n--- در حال اجرای سفارش [${i + 1}/${queue.length}]: ${item.stock} (${item.qty} سهم) ---`);
        logAppend(`⚡ سفارش [${i + 1}/${queue.length}]: استفاده از نشست فعال مرورگر و ثبت سریع...`);
      }
      startLogPolling();

      try {
        const extraParams = {
          keep_browser: true,
          target_epoch: scheduleInfo ? scheduleInfo.target_epoch : undefined,
          target_time: scheduleInfo ? scheduleInfo.target_time : undefined,
        };

        const result = await executeBuyRequest(
          item.stock,
          item.qty,
          activeCancelId,
          { mode: item.price_mode, value: item.price_value },
          extraParams
        );
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
        logAppend("⏳ مکث ۱.۵ ثانیه‌ای قبل از سفارش بعدی صف...");
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
    }

    stopLogPolling();
    activeCancelId = null;
    setRunningState("queue", false);
    hideScheduleBanner();

    showToast("اجرای صف نوبتی به پایان رسید.", true);
    logAppend("\n🏁 پایان کامل صف نوبتی ایزی‌تریدر.");
    alert("تمامی سفارش‌های صف نوبتی پردازش شدند.");
  }

  // Queue Run Click Handler
  async function handleQueueRun() {
    const creds = getCredentials();
    if (!creds.username || !creds.password) {
      showToast("لطفاً نام کاربری و رمز عبور را در بخش ۱ وارد کنید.", false);
      el("et_username").focus();
      return;
    }

    const queue = loadQueue();
    if (queue.length === 0) {
      showToast("صف سفارش‌ها خالی است. ابتدا نمادها را به صف اضافه کنید.", false);
      return;
    }

    // Check Schedule
    const sched = getScheduleSettings();
    if (sched === null) {
      // Validation error in schedule fields
      return;
    }

    if (sched.enabled) {
      const now = Date.now();
      if (now < sched.T_wake) {
        // Case 1: Waiting before the 60s pre-warm window
        startScheduleCountdown(sched, "queue");
        return;
      } else if (now >= sched.T_wake && now < sched.T_target) {
        // Case 2: Inside the 60s prep window -> run immediately with target_epoch
        logAppend(`⚡ در بازه آماده‌سازی و گرم‌کردن کانکشن (کمتر از ۱ دقیقه تا هدف) قرار داریم. شروع فوری صف نوبتی (هدف سرخطی: ${sched.timeStr})...`);
        showToast(`در بازه آماده‌سازی ۶۰ ثانیه پیش از ${sched.timeStr} قرار داریم. شروع فوری صف...`, true);
        const banner = el("et_schedule_banner");
        if (banner) {
          banner.innerHTML = `<div>⚡ <strong>آماده‌سازی فوری صف:</strong> در حال اجرای اتوماسیون جهت ثبت در رأس ساعت [<strong>${escapeHtml(sched.timeStr)}</strong>]</div>`;
          banner.classList.add("active");
        }
        await runQueueExecution({ target_epoch: sched.target_epoch, target_time: sched.target_time });
        return;
      } else {
        // Case 3: now >= T_target -> execute immediately
        logAppend(`⚡ زمان هدف سپری شده یا هم‌اکنون است. اجرای فوری صف...`);
        await runQueueExecution(null);
        return;
      }
    }

    // Direct execution without schedule
    await runQueueExecution(null);
  }

  // Queue Stop
  async function handleQueueStop() {
    if (isScheduledWaiting && scheduledMode === "queue") {
      cancelSchedule("با دکمه توقف صف");
      return;
    }
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

  // Close Browser / Reset Session Handler
  async function handleCloseBrowser() {
    const btn = el("btn_close_browser");
    if (btn) btn.disabled = true;
    logAppend("🌐 در حال ارسال درخواست بستن مرورگر و بازنشانی نشست به سرور...");
    try {
      const res = await fetch("/api/easytrader/reset_session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (res.ok) {
        showToast("پنجره مرورگر و نشست با موفقیت بسته شد.", true);
        logAppend("✅ مرورگر و نشست فعال با موفقیت بسته شد.");
      } else {
        const data = await res.json().catch(() => ({}));
        const msg = data.detail || data.message || `کد خطا: ${res.status}`;
        if (res.status === 404) {
          logAppend("ℹ️ نشست فعالی در سرور یافت نشد یا قبلاً بسته شده است.");
          showToast("نشست مرورگر بازنشانی شد.", true);
        } else {
          showToast("خطا در بستن مرورگر: " + msg, false);
          logAppend("❌ خطا در بستن مرورگر: " + msg);
        }
      }
    } catch (err) {
      showToast("خطای ارتباط با سرور: " + err.message, false);
      logAppend("❌ خطای ارتباط در بستن مرورگر: " + err.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // Initialization
  window.addEventListener("DOMContentLoaded", () => {
    // Initialize Schedule Inputs
    initScheduleInputs();

    // Restore saved credentials if available
    loadSavedCredentials();

    // Auto-save credentials on form modifications
    const formFields = ["et_username", "et_password", "et_stock", "et_qty", "et_price", "et_schedule_time"];
    formFields.forEach((id) => {
      const inputEl = el(id);
      if (inputEl) {
        inputEl.addEventListener("input", saveCredentialsIfRemembered);
        inputEl.addEventListener("change", saveCredentialsIfRemembered);
      }
    });

    document.querySelectorAll('input[name="et_price_mode"]').forEach((r) => {
      r.addEventListener("change", saveCredentialsIfRemembered);
    });

    el("et_remember_me")?.addEventListener("change", saveCredentialsIfRemembered);

    // Active press feedback for blast button
    const blastBtn = el("btn_buy_single");
    if (blastBtn) {
      blastBtn.addEventListener("mousedown", () => blastBtn.classList.add("btn-pressed"));
      blastBtn.addEventListener("mouseup", () => blastBtn.classList.remove("btn-pressed"));
      blastBtn.addEventListener("mouseleave", () => blastBtn.classList.remove("btn-pressed"));
      blastBtn.addEventListener("touchstart", () => blastBtn.classList.add("btn-pressed"), { passive: true });
      blastBtn.addEventListener("touchend", () => blastBtn.classList.remove("btn-pressed"), { passive: true });
    }

    // Buttons
    el("btn_buy_single")?.addEventListener("click", handleSingleBuy);
    el("btn_stop_single")?.addEventListener("click", handleSingleStop);
    el("btn_add_to_queue")?.addEventListener("click", addToQueue);
    el("btn_queue_run")?.addEventListener("click", handleQueueRun);
    el("btn_queue_stop")?.addEventListener("click", handleQueueStop);
    el("btn_queue_clear")?.addEventListener("click", clearQueue);
    el("btn_close_browser")?.addEventListener("click", handleCloseBrowser);
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
    el("et_stock")?.addEventListener("keydown", (e) => {
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

    // Initial render of queue & button states
    renderQueue();
    updateButtonsState();
  });
})();
