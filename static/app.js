const state = {
  config: null,
  selectedId: null,
  automation: {
    sessionId: null,
    recording: false,
    playbackCancelId: /** @type {string | null} */ (null),
    playbackRunning: false,
  },
};

/** @param {string} id */
function el(id) {
  return document.getElementById(id);
}

/** @param {string} msg */
function toast(msg, ok = true, hideMs = 5200) {
  const box = el("toast");
  if (!box) {
    console.warn("[toast fallback]", ok ? "[ok]" : "[err]", msg);
    return;
  }
  box.innerHTML = `<div class="banner ${ok ? "ok" : "err"}">${escapeHtml(msg)}</div>`;
  window.clearTimeout(toast._t);
  toast._t = window.setTimeout(() => {
    box.innerHTML = "";
  }, hideMs);
}

/** @param {string} s */
function escapeHtml(s) {
  const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" };
  return String(s).replace(/[&<>"']/g, (c) => /** @type {string} */ (map[c]));
}

/** @param {string} v */
function escapeAttr(v) {
  return escapeHtml(v).replace(/"/g, "&quot;");
}

function uuid() {
  return crypto.randomUUID();
}

function _formatApiError(payload) {
  if (!payload) return "";
  const d = payload.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join(" | ");
  if (typeof d === "object" && d) return JSON.stringify(d);
  if (payload.error) return typeof payload.error === "string" ? payload.error : JSON.stringify(payload.error);
  return JSON.stringify(payload);
}

async function apiJson(url, options = {}, retries = 3) {
  let lastErr = null;
  for (let i = 0; i <= retries; i++) {
    try {
      const res = await fetch(url, options);
      const raw = await res.text();
      let data = {};
      if (raw) {
        try {
          data = JSON.parse(raw);
        } catch {
          data = { detail: raw };
        }
      }
      if (!res.ok) throw new Error(_formatApiError(data) || `HTTP ${res.status}`);
      return data;
    } catch (err) {
      lastErr = err;
      if (i < retries) {
        await new Promise((r) => setTimeout(r, 1200));
      }
    }
  }
  throw lastErr;
}

function emptyWorkflow() {
  return {
    id: uuid(),
    name: "پروژهٔ بدون عنوان",
    base_url: "",
    check_url: "",
    monitor_this: true,
    notes: "",
    sections: [
      {
        title: "بخش نمونه",
        steps: [
          {
            title: "مثلاً روی ورود کلیک کن",
            instruction:
              'همین متن‌ها فقط یادداشتٔ شخصی تو برای وقت کار با مرورگر است. نمونه: «۱) روی ورود کلیک کن ۲) کد تأیید پیامک را دستی وارد کن ۳) از منوی بالا ثبت را باز کن». این برنامه این مراحل را کلیک یا اجرا نمی‌کند؛ خودت در مرورگر انجامشان می‌دهی.',
            selector_hint: "",
          },
        ],
      },
    ],
  };
}

/** @returns {any | null} */
function selectedWorkflow() {
  if (!state.config || !state.selectedId) return null;
  return (state.config.workflows || []).find((w) => w.id === state.selectedId) || null;
}

/**
 * بازتاب ورودی‌های تخت و متناظر هر بخش/مرحله از DOM به آبجکت جریان کاری منتخب.
 * @returns {boolean}
 */
function flushEditorToWorkflow() {
  const wf = selectedWorkflow();
  if (!wf) return false;

  wf.name = /** @type {HTMLInputElement} */ (el(`wf_name_${wf.id}`)).value.trim() || wf.id.slice(0, 8);
  wf.base_url = /** @type {HTMLInputElement} */ (el(`wf_base_${wf.id}`)).value.trim();
  wf.check_url = /** @type {HTMLInputElement} */ (el(`wf_check_${wf.id}`)).value.trim();
  wf.notes = /** @type {HTMLTextAreaElement} */ (el(`wf_notes_${wf.id}`)).value;
  wf.monitor_this = /** @type {HTMLInputElement} */ (el(`wf_monitor_${wf.id}`)).checked;

  (wf.sections || []).forEach((sec, si) => {
    const stEl = /** @type {HTMLInputElement | null} */ (document.getElementById(`sec_title_${wf.id}_${si}`));
    if (stEl) sec.title = stEl.value;
    (sec.steps || []).forEach((step, ti) => {
      const tEl = /** @type {HTMLInputElement | null} */ (document.getElementById(`step_title_${wf.id}_${si}_${ti}`));
      const iEl = /** @type {HTMLTextAreaElement | null} */ (document.getElementById(`step_inst_${wf.id}_${si}_${ti}`));
      const sEl = /** @type {HTMLInputElement | null} */ (document.getElementById(`step_sel_${wf.id}_${si}_${ti}`));
      if (tEl) step.title = tEl.value;
      if (iEl) step.instruction = iEl.value;
      if (sEl) step.selector_hint = sEl.value;
    });
  });
  return true;
}

function stashCurrentEditorToState() {
  flushEditorToWorkflow();
}

function renderWorkflowList() {
  const holder = el("wf_list");
  holder.innerHTML = "";
  const wfs = [...(state.config?.workflows || [])];

  wfs.forEach((wf, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "wf-item" + (wf.id === state.selectedId ? " active" : "");
    b.textContent = `${i + 1}. ${wf.name || wf.id}`;
    b.addEventListener("click", () => {
      stashCurrentEditorToState();
      state.selectedId = wf.id;
      renderWorkflowList();
      renderWorkflowEditor();
    });
    holder.appendChild(b);
  });

  const delRow = document.createElement("div");
  delRow.className = "row";
  delRow.style.marginTop = "10px";
  const btnDel = document.createElement("button");
  btnDel.type = "button";
  btnDel.className = "danger small";
  btnDel.textContent = "حذف پروژهٔ منتخب";
  btnDel.disabled = !state.selectedId;
  btnDel.addEventListener("click", () => removeSelectedWorkflow());
  delRow.appendChild(btnDel);
  holder.appendChild(delRow);
}

function removeSelectedWorkflow() {
  stashCurrentEditorToState();
  if (!state.config || !state.selectedId) return;
  state.config.workflows = (state.config.workflows || []).filter((w) => w.id !== state.selectedId);
  const next = state.config.workflows[0];
  state.selectedId = next ? next.id : null;
  renderWorkflowList();
  renderWorkflowEditor();
  toast("از حافظهٔ موقت حذف شد؛ برای ثبت روی دیسک «ذخیرهٔ همه چیز» را بزنید.", true);
}

function renderWorkflowEditor() {
  const box = el("wf_editor");
  if (!state.config) {
    box.innerHTML = '<p class="muted-small">پیکربندی بارگذاری نشده.</p>';
    return;
  }
  if (!state.selectedId) {
    box.innerHTML =
      '<p class="muted-small">از همین کارتی که بالاست دکمهٔ <strong>پروژهٔ جدید (یک وب‌گاه دیگر)</strong> را بزن؛ اگر قبلاً چیزی ذخیره کرده‌ای اسم پروژه را هم از همان فهرست نازکِ کنار دستش انتخاب کن.</p>';
    return;
  }

  const wf = selectedWorkflow();
  if (!wf) {
    box.innerHTML = '<p class="muted-small">شناسه به‌هم خورده؛ از فهرستِ همان ستونٔ نازک دوباره روی اسم پروژه کلیک کن.</p>';
    return;
  }

  const wfId = wf.id;
  wf.sections = Array.isArray(wf.sections) && wf.sections.length ? wf.sections : [{ title: "", steps: [{ title: "", instruction: "", selector_hint: "" }] }];
  wf.sections.forEach((sec) => {
    sec.steps =
      Array.isArray(sec.steps) && sec.steps.length
        ? sec.steps
        : [{ title: "", instruction: "", selector_hint: "" }];
  });

  /** @type {string[]} */
  const parts = [];
  parts.push(
    `<div class="editor-lead"><strong>اول این پنل را آرام آرام پر کن؛</strong> متن هر مرحله فقط کمکٔ حافظهٔ تو برای وقتی است که وب باز است؛ کلیک یا پسورد اینجا اجرا نمی‌شوند. هر وقت تغییری دادی پایین صفحه <strong>ذخیرهٔ همهٔ چیز روی دیسک</strong> را بزن.</div>`,
  );
  parts.push(`<div class="field"><label for="wf_name_${wfId}">نام این پروژه (تو برای خودت)</label>`);
  parts.push(
    `<span class="field-hint">نام کوتاه که به خاطرت بماند؛ مثل «بانک فلان» یا «پرتال ثبت». همین عنوان در ستونٔ کناری فهرست دیده می‌شود.</span>`,
  );
  parts.push(`<input id="wf_name_${wfId}" type="text" value="${escapeAttr(wf.name || "")}" /></div>`);

  parts.push('<div class="grid-2">');
  parts.push(`<div class="field"><label for="wf_base_${wfId}">آدرس پایه (همان خط آدرسی که در مرورگر می‌توانی باز کنی)</label>`);
  parts.push(
    `<span class="field-hint">کل آدرس را از نوار نشانی کپی کن؛ معمولاً با https شروع می‌شود. اول می‌توانی با مرورگر امتحان کنی باز شود؛ اینجا فقط پیست کن.</span>`,
  );
  parts.push(`<input id="wf_base_${wfId}" type="text" value="${escapeAttr(wf.base_url || "")}" placeholder="https://example.com/..." /></div>`);
  parts.push(`<div class="field"><label for="wf_check_${wfId}">آدرس ویژهٔ چک شبکه (اغلب همین اول کافی؛ خالی بگذاری همان پایه بارها چک می‌شود)</label>`);
  parts.push(
    `<span class="field-hint">اغلب همین نشانی عمومی پیش‌فرض کافی است؛ فقط وقتی لازم داری نشانیٔ جدا برای آزمایش بگذار، همین ستون را پر کن. هر «چک» اینجا یعنی «آیا وب‌گاه در لایٔهٔ عمومی از این رایانه جواب می‌دهد»؛ دلیل بر باز بودن حساب شخصیٔ تو نیست؛ برای حساب واقعی باز هم باید دستی با مرورگر پیش بروی.</span>`,
  );
  parts.push(`<input id="wf_check_${wfId}" type="text" value="${escapeAttr(wf.check_url || "")}" placeholder="خالی مانده = از آدرس پایه استفاده کن" /></div>`);
  parts.push("</div>");

  parts.push('<div class="row" style="align-items:flex-start;flex-wrap:wrap;gap:12px">');
  parts.push(
    `<label class="toggle"><input id="wf_monitor_${wfId}" type="checkbox" ${wf.monitor_this ? "checked" : ""} /><span>این پروژه وقتی در بخش ۲ همین برگهٔ «مانیتورینگ فعال…» روشن است، در دور چکٔ پشت‌سرهم شامل شود</span></label>`,
  );
  parts.push(`<button type="button" class="small" id="btn_probe_${wfId}">همین الان فقط یک‌بار شبکهٔ این آدرس را آزمایش کن</button>`);
  parts.push("</div>");
  parts.push(
    '<p class="muted-small probe-explain"><strong>نکته:</strong> اگر در بخشٔ <strong>۲ (بله)</strong> گزینهٔ «مانیتورینگ فعال…» خاموش باشد، هر پروژه با هر تکی هم در پشت‌زمینه کاری پیش نمی‌رود؛ آن گزینه موتور اصلی است، این سوئیچ فقط می‌گوید هر پروژه داخل همین طرح باشد یا نه.</p>',
    '<p class="muted-small probe-explain">دکمهٔ آزمایشٔ همین بخش فقط الان از همین دستگاه برای همان آدرس درخواست می‌فرستد و عددٔ وضعیت وب را نشان می‌دهد؛ یعنی معمولاً «صفحهٔ عمومی باز است یا نه»؛ به‌معنای «حتماً داخل حساب شدی نیست؛ فقط خبر قطع و وصل شبکهٔ همان نشانی کمک می‌کند».</p>',
  );

  parts.push(`<div class="field"><label for="wf_notes_${wfId}">یادداشت آزاد هر نکتهٔ کوتاهی که دوست داری اینجا بماند.</label>`);
  parts.push(
    `<span class="field-hint">برای وقت‌های شلوغ سایت؛ مرورگر خاص؛ شماره پشتیبان؛ هر نکتهٔ فنی که می‌ترسی یادت برود؛ خالی مانَد اشکالی ندارد.</span>`,
  );
  parts.push(`<textarea id="wf_notes_${wfId}" placeholder="مثلاً ساعت باز بودن اداری؛ یا شماره پشتیبانی.">${escapeHtml(wf.notes || "")}</textarea></div>`);

  wf.sections.forEach((sec, si) => {
    parts.push(`<div class="section-head" data-sec="${si}">`);
    parts.push(`<div class="row" style="justify-content:space-between">`);
    parts.push(`<div class="field" style="flex:1;min-width:200px"><label for="sec_title_${wfId}_${si}">عنوان همین قطعهٔ فرایند؛ مثل «اولین ورود» یا «صفحهٔ فرم‌ها»</label>`);
    parts.push(
      `<span class="field-hint">هر عنوانی برای آن‌که یاد بمانی الان «کدام قسمت کار»؛ حتی عنوان تبٔ مرورگر یا یک کلمهٔ ساده مانند «ورود» کافی است.</span>`,
    );
    parts.push(`<input id="sec_title_${wfId}_${si}" type="text" value="${escapeAttr(sec.title || "")}" /></div>`);
    parts.push(`<button type="button" class="small danger" data-act="rm-sec" data-si="${si}">حذف بخش</button>`);
    parts.push(`</div>`);

    sec.steps.forEach((step, ti) => {
      parts.push(`<div class="step-box" data-step="${ti}">`);
      parts.push(`<div class="row" style="justify-content:space-between">`);
      parts.push(`<strong class="muted-small">مرحلهٔ ${ti + 1}</strong>`);
      parts.push(`<button type="button" class="small danger" data-act="rm-step" data-si="${si}" data-ti="${ti}">حذف مرحله</button>`);
      parts.push(`</div>`);
      parts.push(`<div class="grid-2">`);
      parts.push(`<div class="field"><label for="step_title_${wfId}_${si}_${ti}">عنوان دو–سه‌کلمه‌ای همین مرحله</label>`);
      parts.push(
        `<span class="field-hint">برای وقتی که چند ده مرحله داری؛ اگر الان لازمت نیست خالی باشد اشکالی ندارد.</span>`,
      );
      parts.push(`<input id="step_title_${wfId}_${si}_${ti}" type="text" value="${escapeAttr(step.title || "")}" /></div>`);
      parts.push(`<div class="field"><label for="step_sel_${wfId}_${si}_${ti}">یادداشت فنی وب (selector)؛ کاملاً اختیاری</label>`);
      parts.push(
        `<span class="field-hint">اگر شناسهٔ المان وب را بلدی (مثلاً برای توسعه‌دهندگان <code class="mono">#btn-login</code>) اینجا نگه دار؛ اگر نه خالی باشد؛ مهم جعبهٔ «دستور این مرحله» پایین است.</span>`,
      );
      parts.push(`<input id="step_sel_${wfId}_${si}_${ti}" type="text" placeholder="#id یا data-testid یا div.main" value="${escapeAttr(step.selector_hint || "")}" /></div>`);
      parts.push(`</div>`);
      parts.push(`<div class="field"><label for="step_inst_${wfId}_${si}_${ti}">دستور این مرحله را گام‌به‌گام بنویس (فقط متن فارسی؛ این برنامه اجراش نمی‌کند)</label>`);
      parts.push(
        `<span class="field-hint">مثلاً «۱) در جعبهٔ موبایل شماره را بگذار ۲) روی تأیید آبی کلیک کن». این‌ها یادداشت برای مرورگرند؛ کلیک خودکار از این ابزار وجود ندارد.</span>`,
      );
      parts.push(`<textarea id="step_inst_${wfId}_${si}_${ti}" placeholder="۱) کلیک ورود؛ ۲) کد تأیید SMS را دستی در قسمت تأیید بگذاری">${escapeHtml(step.instruction || "")}</textarea></div>`);
      parts.push(`</div>`);
    });

    parts.push(`<div class="row">`);
    parts.push(`<button type="button" class="small" data-act="add-step" data-si="${si}">+ مرحله در همین بخش</button>`);
    parts.push(`</div>`);

    parts.push(`</div>`);
  });

  parts.push(`<div class="row">`);
  parts.push(`<button type="button" class="small" data-act="add-sec">+ بخش جدید</button>`);
  parts.push(`</div>`);

  box.innerHTML = parts.join("");

  el(`btn_probe_${wfId}`).addEventListener("click", async () => {
    stashCurrentEditorToState();
    const cur = selectedWorkflow();
    const url = (cur?.check_url || cur?.base_url || "").trim();
    if (!url) {
      toast("آدرس خالی است.", false);
      return;
    }
    const r = await fetch("/api/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const j = /** @type {any} */ (await r.json().catch(() => ({})));
    if (!r.ok) {
      toast("خطا: " + (j.detail || JSON.stringify(j)), false);
      return;
    }
    toast(j.ok ? `پاسخ: ${j.detail}` : `ناموفق: ${j.detail}`, j.ok);
  });
}

/** @param {MouseEvent} e */
function handleWorkflowEditorClick(e) {
  const t = /** @type {HTMLElement | null} */ (e.target);
  const btn = t && t.closest ? /** @type {HTMLElement | null} */ (t.closest("[data-act]")) : null;
  if (!btn) return;

  stashCurrentEditorToState();
  const cur = selectedWorkflow();
  if (!cur) return;

  const act = btn.getAttribute("data-act");

  if (act === "add-sec") {
    cur.sections.push({
      title: "بخش جدید",
      steps: [{ title: "مرحله", instruction: "", selector_hint: "" }],
    });
    renderWorkflowEditor();
    return;
  }

  const si = Number(btn.getAttribute("data-si") || -1);

  if (act === "rm-sec" && si >= 0) {
    cur.sections.splice(si, 1);
    if (!cur.sections.length) {
      cur.sections.push({ title: "", steps: [{ title: "", instruction: "", selector_hint: "" }] });
    }
    renderWorkflowEditor();
    return;
  }

  if (act === "add-step" && si >= 0 && cur.sections[si]) {
    cur.sections[si].steps.push({ title: "", instruction: "", selector_hint: "" });
    renderWorkflowEditor();
    return;
  }

  if (act === "rm-step" && si >= 0) {
    const ti = Number(btn.getAttribute("data-ti") || -1);
    const sec = cur.sections[si];
    if (!sec || ti < 0) return;
    sec.steps.splice(ti, 1);
    if (!sec.steps.length) {
      sec.steps.push({ title: "", instruction: "", selector_hint: "" });
    }
    renderWorkflowEditor();
  }
}

async function loadConfigFromServer() {
  const r = await fetch("/api/config");
  if (!r.ok) throw new Error(await r.text());
  state.config = await r.json();

  const ba = state.config.bale || {};
  const ea = state.config.eitaa || {};
  const tgLegacy = state.config.telegram || {};
  el("bale_bot_token").value = ba.bot_token || ea.token || tgLegacy.bot_token || "";
  el("bale_chat_id").value = String(ba.chat_id || ea.chat_id || tgLegacy.chat_id || "");
  el("poll_interval").value = Math.max(15, Number(state.config.poll_interval_seconds) || 60);
  el("monitoring_enabled").checked = !!state.config.monitoring_enabled;

  if (!Array.isArray(state.config.scenario_queue)) {
    state.config.scenario_queue = [];
  }
  renderScenarioQueue();
}

async function persistConfigFromForm() {
  /** @type {any} */
  const body = {
    bale: {
      bot_token: el("bale_bot_token").value.trim(),
      chat_id: el("bale_chat_id").value.trim(),
    },
    poll_interval_seconds: Number(el("poll_interval").value || 60),
    monitoring_enabled: el("monitoring_enabled").checked,
    workflows: [...(state.config?.workflows || [])],
    scenario_queue: [...(state.config?.scenario_queue || [])],
  };

  const r = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!r.ok) {
    toast("ذخیره ناموفق: " + (await r.text()), false);
    return;
  }

  toast("ذخیره شد و مانیتور در صورت فعال بودن به‌روزرسانی شد.", true);
  await loadConfigFromServer().catch(() => {});
}

async function testBale() {
  stashCurrentEditorToState();
  const r = await fetch("/api/bale/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      bot_token: el("bale_bot_token").value.trim(),
      chat_id: el("bale_chat_id").value.trim(),
    }),
  });
  const t = await r.text();
  if (!r.ok) {
    toast("بله: " + t, false);
    return;
  }
  toast("پیام آزمایشی به بله فرستاده شد.", true);
}

function addWorkflow() {
  if (!state.config) return;
  stashCurrentEditorToState();
  state.config.workflows.push(emptyWorkflow());
  state.selectedId = state.config.workflows[state.config.workflows.length - 1].id;
  renderWorkflowList();
  renderWorkflowEditor();
}

function bindImportExport() {
  el("btn_export").addEventListener("click", () => {
    stashCurrentEditorToState();
    const blob = new Blob([JSON.stringify(state.config, null, 2)], { type: "application/json;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "site-workflow-config.json";
    a.click();
    URL.revokeObjectURL(a.href);
  });

  el("btn_import").addEventListener("click", () => {
    el("import_file").click();
  });

  el("import_file").addEventListener("change", async (ev) => {
    const inp = /** @type {HTMLInputElement} */ (ev.target);
    const f = inp.files?.[0];
    if (!f) return;
    const txt = await f.text();
    try {
      const data = JSON.parse(txt);
      if (!Array.isArray(data.workflows) && !Array.isArray(data.scenario_queue)) {
        throw new Error("فایل باید workflows یا scenario_queue داشته باشد");
      }
      if (!Array.isArray(data.workflows)) data.workflows = [];
      if (!Array.isArray(data.scenario_queue)) data.scenario_queue = [];
      if (!data.bale && !data.eitaa && !data.telegram) throw new Error("فایل باید بخش bale یا eitaa / telegram قدیمی داشته باشد");
      stashCurrentEditorToState();
      if (!data.bale && data.eitaa) {
        data.bale = {
          bot_token: String(data.eitaa.token || "").trim(),
          chat_id: String(data.eitaa.chat_id || "").trim(),
        };
      }
      if ((!data.bale || !(data.bale.bot_token || "").trim()) && data.telegram) {
        data.bale = {
          bot_token: String(data.telegram.bot_token || "").trim(),
          chat_id: String(data.telegram.chat_id || "").trim(),
        };
      }
      state.config = data;
      const bb = state.config.bale || {};
      const ee = state.config.eitaa || {};
      const tt = state.config.telegram || {};
      el("bale_bot_token").value = bb.bot_token || ee.token || tt.bot_token || "";
      el("bale_chat_id").value = String(bb.chat_id || ee.chat_id || tt.chat_id || "");
      el("poll_interval").value = Math.max(15, Number(state.config.poll_interval_seconds) || 60);
      el("monitoring_enabled").checked = !!state.config.monitoring_enabled;
      renderScenarioQueue();
      toast("وارد شد (موقت). حتماً ذخیره را بزنید.", true);
    } catch (e) {
      toast(String(/** @type {Error} */ (e).message || e), false);
    }
    inp.value = "";
  });
}

function setAutoStatus(text) {
  const n = el("auto_status");
  if (n) n.textContent = text;
}

function setAutoLog(text) {
  const box = el("auto_log");
  if (!box) return;
  box.textContent = text;
}

function appendAutoLog(text) {
  const box = el("auto_log");
  if (!box) return;
  box.textContent += `\n${text}`;
  box.scrollTop = box.scrollHeight;
}

/** توضیح ساده برای کاربر غیرفنی؛ بدون متن خام Playwright */
function friendlyPlaybackFailureReason(raw) {
  const s = String(raw || "");
  if (/net::ERR_CONNECTION_CLOSED/i.test(s)) {
    return "ارتباط با سایت ناگهان قطع شد؛ اینترنت، فیلترشکن یا بعداً دوباره امتحان کن.";
  }
  if (/net::ERR_CONNECTION_(TIMED_OUT|RESET|REFUSED)/i.test(s) || /\bTimeoutError\b/i.test(s) || /timeout/i.test(s)) {
    return "سایت جواب نداد یا خیلی دیر جواب داد؛ بعداً دوباره امتحان کن.";
  }
  if (
    /net::ERR_NAME_NOT_RESOLVED/i.test(s) ||
    /\b(Name not resolved|getaddrinfo|nodename nor servname)/i.test(s)
  ) {
    return "DNS یا شبکه قطع است یا دیر جواب می‌دهد؛ اگر آدرس را در مرورگر معمولی باز می‌کنی، خود اجرا هم تا وصل‌شدن دوباره تلاش می‌کند؛ املای نشانی را چک کن.";
  }
  if (/net::ERR_CERT|SSL|certificate/i.test(s)) {
    return "مرورگر دربارهٔ امنیت سایت هشدار می‌دهد؛ با مرورگر معمولی هم تست کن.";
  }
  if (/Page\.goto/i.test(s) || /navigating to/i.test(s)) {
    return "صفحهٔ وب باز نشد یا نیمه‌کاره ماند.";
  }
  return "این مرحله کامل نشد؛ دوباره امتحان کن یا از پشتیبان سایت بپرس.";
}

function getScenarioFromEditor() {
  const txt = el("auto_scenario").value.trim();
  if (!txt) throw new Error("سناریو خالی است");
  const scen = JSON.parse(txt);
  const turboEl = el("auto_turbo_mode");
  if (turboEl) {
    scen.turbo_mode = !!turboEl.checked;
  }
  const refreshMsEl = el("auto_refresh_ms");
  if (refreshMsEl) {
    const rVal = parseInt(refreshMsEl.value, 10);
    scen.refresh_interval_ms = Number.isFinite(rVal) ? rVal : 80;
  }
  const keepOpenEl = el("auto_keep_open");
  if (keepOpenEl) {
    const ko = !!keepOpenEl.checked;
    scen.keep_open = ko;
    scen.keep_open_on_missing = ko;
    scen.keep_browser_on_failure = ko;
  }
  const pauseEl = el("auto_pause_steps_sec");
  if (pauseEl) {
    if (scen.turbo_mode) {
      scen.pause_between_steps_ms = 0;
    } else {
      const pauseSec = parseFloat(String(pauseEl.value || "0"));
      if (Number.isFinite(pauseSec) && pauseSec >= 0) {
        scen.pause_between_steps_ms = Math.round(pauseSec * 1000);
      }
    }
  }
  return scen;
}

function getRunsFromEditor() {
  const txt = el("auto_runs").value.trim();
  if (!txt) return null;
  const x = JSON.parse(txt);
  if (!Array.isArray(x)) throw new Error("runs باید آرایه باشد");
  return x;
}

function setScenarioToEditor(scen) {
  el("auto_scenario").value = JSON.stringify(scen, null, 2);
  if (Array.isArray(scen?.runs)) {
    el("auto_runs").value = JSON.stringify(scen.runs, null, 2);
  }
  const pMs = Number(scen?.pause_between_steps_ms);
  const pauseEl = el("auto_pause_steps_sec");
  if (pauseEl) {
    if (Number.isFinite(pMs) && pMs >= 0) {
      pauseEl.value = String(pMs / 1000);
    } else {
      pauseEl.value = "0";
    }
  }
  const hS = Number(scen?.playback_health_refresh_interval_s);
  const hEl = el("auto_health_refresh_sec");
  if (hEl) {
    if (Number.isFinite(hS) && hS >= 0) {
      hEl.value = String(hS);
    } else {
      hEl.value = "30";
    }
  }
  let su = typeof scen?.start_url === "string" ? scen.start_url.trim() : "";
  if (!su && Array.isArray(scen?.steps)) {
    const firstGoto = scen.steps.find((s) => (s?.type || "").toLowerCase() === "goto" && s?.url);
    if (firstGoto) {
      su = String(firstGoto.url || "").trim();
    }
  }
  const playUrlEl = el("auto_playback_url");
  if (playUrlEl) {
    playUrlEl.value = su;
  }
  const autoUrlEl = el("auto_url");
  if (autoUrlEl) {
    autoUrlEl.value = su;
  }
  const mk = el("auto_show_marker");
  if (mk) mk.checked = scen.show_click_marker !== false;

  const turboEl = el("auto_turbo_mode");
  const isTurbo = !!scen?.turbo_mode;
  if (turboEl) {
    turboEl.checked = isTurbo;
  }
  if (pauseEl) {
    if (isTurbo) {
      pauseEl.value = "0";
      pauseEl.disabled = true;
    } else {
      pauseEl.disabled = false;
    }
  }
  const refreshMsEl = el("auto_refresh_ms");
  if (refreshMsEl) {
    const rMs = scen ? Number(scen.refresh_interval_ms) : NaN;
    if (Number.isFinite(rMs) && rMs >= 20 && rMs <= 2000) {
      refreshMsEl.value = String(rMs);
    } else {
      refreshMsEl.value = "80";
    }
  }
  const keepOpenEl = el("auto_keep_open");
  if (keepOpenEl) {
    if (scen?.keep_open !== undefined) {
      keepOpenEl.checked = !!scen.keep_open;
    } else if (scen?.keep_open_on_missing !== undefined) {
      keepOpenEl.checked = !!scen.keep_open_on_missing;
    } else if (scen?.keep_browser_on_failure !== undefined) {
      keepOpenEl.checked = !!scen.keep_browser_on_failure;
    } else {
      keepOpenEl.checked = true;
    }
  }
}

async function reloadScriptsList() {
  const sel = el("auto_scripts_list");
  if (!sel) return;
  const out = await apiJson("/api/automation/scripts");
  sel.innerHTML = "";
  const items = out.items || [];
  if (!items.length) {
    const op = document.createElement("option");
    op.value = "";
    op.textContent = "سناریوی ذخیره‌شده‌ای نیست";
    sel.appendChild(op);
    return;
  }
  for (const it of items) {
    const op = document.createElement("option");
    op.value = it.id;
    op.textContent = `${it.name} (${it.id})`;
    sel.appendChild(op);
  }
  // If there are items, auto-select and auto-load the first scenario into the editor
  if (items.length > 0 && !el("auto_scenario").value.trim()) {
    const firstId = items[0].id;
    sel.value = firstId;
    apiJson(`/api/automation/scripts/${firstId}`).then((out) => {
      setScenarioToEditor(out.script);
      const meta = out.script.meta || {};
      el("auto_script_id").value = meta.id || firstId;
      el("auto_script_name").value = meta.name || "";
    }).catch(() => {});
  }
  sel.addEventListener("change", async () => {
    const fid = sel.value;
    if (!fid) return;
    try {
      const out = await apiJson(`/api/automation/scripts/${fid}`);
      setScenarioToEditor(out.script);
      const meta = out.script.meta || {};
      el("auto_script_id").value = meta.id || fid;
      el("auto_script_name").value = meta.name || "";
      toast("سناریو بارگذاری شد.", true);
    } catch (e) {
      toast(`بارگذاری ناموفق: ${e.message || e}`, false);
    }
  });
  syncQueueScriptPickers(items);
}

function syncQueueScriptPickers(items) {
  const sel = el("queue_pick_script");
  if (!sel) return;
  sel.innerHTML = "";
  if (!items.length) {
    const op = document.createElement("option");
    op.value = "";
    op.textContent = "اول در بخش ۴ سناریو ذخیره کن";
    sel.appendChild(op);
    return;
  }
  for (const it of items) {
    const op = document.createElement("option");
    op.value = it.id;
    op.textContent = `${it.name} (${it.id})`;
    sel.appendChild(op);
  }
}

function ensureScenarioQueue() {
  if (!state.config) return;
  if (!Array.isArray(state.config.scenario_queue)) {
    state.config.scenario_queue = [];
  }
}

function renderScenarioQueue() {
  const holder = el("queue_list");
  const empty = el("queue_empty");
  if (!holder || !state.config) return;
  ensureScenarioQueue();
  const q = state.config.scenario_queue;
  holder.innerHTML = "";
  if (empty) {
    empty.style.display = q.length ? "none" : "block";
  }
  q.forEach((item, i) => {
    const row = document.createElement("div");
    row.className = "queue-item";
    const title = item.label || item.script_id || `سناریو ${i + 1}`;
    const su = item.start_url ? ` — ${item.start_url}` : "";
    row.innerHTML = `<span class="queue-idx">${i + 1}.</span>
      <span class="queue-title">${escapeHtml(title)}</span>
      <span class="queue-meta muted-small mono">${escapeHtml(item.script_id || "")}${escapeHtml(su)}</span>
      <span class="queue-actions">
        <button type="button" class="small" data-q-up="${i}" title="بالا">↑</button>
        <button type="button" class="small" data-q-down="${i}" title="پایین">↓</button>
        <button type="button" class="small danger" data-q-rm="${i}">حذف</button>
      </span>`;
    holder.appendChild(row);
  });
}

function moveQueueItem(index, dir) {
  ensureScenarioQueue();
  const q = state.config.scenario_queue;
  const j = index + dir;
  if (j < 0 || j >= q.length) return;
  const t = q[index];
  q[index] = q[j];
  q[j] = t;
  renderScenarioQueue();
}

function bindQueueUI() {
  const list = el("queue_list");
  if (list) {
    list.addEventListener("click", (e) => {
      const t = /** @type {HTMLElement} */ (e.target);
      const up = t.closest("[data-q-up]");
      const down = t.closest("[data-q-down]");
      const rm = t.closest("[data-q-rm]");
      if (up) {
        moveQueueItem(Number(up.getAttribute("data-q-up")), -1);
        return;
      }
      if (down) {
        moveQueueItem(Number(down.getAttribute("data-q-down")), 1);
        return;
      }
      if (rm) {
        const i = Number(rm.getAttribute("data-q-rm"));
        ensureScenarioQueue();
        state.config.scenario_queue.splice(i, 1);
        renderScenarioQueue();
      }
    });
  }

  el("btn_queue_add")?.addEventListener("click", () => {
    if (!state.config) return;
    const sid = String(el("queue_pick_script")?.value || "").trim();
    if (!sid) {
      toast("یک سناریوی ذخیره‌شده انتخاب کن.", false);
      return;
    }
    ensureScenarioQueue();
    const pick = el("queue_pick_script");
    const opt = pick?.selectedOptions?.[0];
    const label = opt ? String(opt.textContent || sid).replace(/\s*\([^)]*\)\s*$/, "").trim() : sid;
    state.config.scenario_queue.push({
      id: uuid(),
      script_id: sid,
      label,
      start_url: String(el("queue_item_start_url")?.value || "").trim(),
    });
    if (el("queue_item_start_url")) el("queue_item_start_url").value = "";
    renderScenarioQueue();
    toast("به صف اضافه شد.", true);
  });

  el("btn_queue_clear")?.addEventListener("click", () => {
    if (!state.config) return;
    state.config.scenario_queue = [];
    renderScenarioQueue();
    toast("صف خالی شد.", true);
  });

  const btnRun = el("btn_queue_run");
  const btnAbort = el("btn_queue_abort");
  const logBox = el("queue_log");

  const setQueueLog = (text) => {
    if (logBox) logBox.textContent = text;
  };
  const appendQueueLog = (text) => {
    if (!logBox) return;
    logBox.textContent += `\n${text}`;
    logBox.scrollTop = logBox.scrollHeight;
  };

  btnRun?.addEventListener("click", async () => {
    if (state.automation.playbackRunning) {
      toast("یک اجرا در جریان است؛ صبر کن یا توقف بزن.", false);
      return;
    }
    ensureScenarioQueue();
    const q = state.config.scenario_queue || [];
    if (!q.length) {
      toast("صف خالی است؛ سناریو اضافه کن.", false);
      return;
    }
    const items = q.map((row) => ({
      script_id: row.script_id,
      label: row.label || row.script_id,
      start_url: row.start_url || "",
    }));
    let finishedClean = false;
    try {
      state.automation.playbackRunning = true;
      if (btnRun) btnRun.disabled = true;
      if (btnAbort) btnAbort.disabled = false;
      const pauseSec = parseFloat(String(el("auto_pause_steps_sec")?.value || "0"));
      const refreshSec = parseFloat(String(el("auto_health_refresh_sec")?.value || "30"));
      const pauseBetween = parseFloat(String(el("queue_pause_scenarios_sec")?.value || "2.5"));
      const payload = {
        items,
        cancellation_id: uuid(),
        pause_between_scenarios_s: Number.isFinite(pauseBetween) ? pauseBetween : 2.5,
        stop_on_first_error: !!el("queue_stop_on_error")?.checked,
        notify_bale: !(el("auto_notify_bale") && !el("auto_notify_bale").checked),
        bale_bot_token: el("bale_bot_token")?.value.trim(),
        bale_chat_id: el("bale_chat_id")?.value.trim(),
      };
      if (Number.isFinite(pauseSec) && pauseSec >= 0) {
        payload.pause_between_steps_ms = Math.round(pauseSec * 1000);
      }
      if (Number.isFinite(refreshSec) && refreshSec >= 0) {
        payload.playback_health_refresh_interval_s = refreshSec;
      }
      state.automation.playbackCancelId = payload.cancellation_id;
      setQueueLog(
        "اجرای صف شروع شد. یک مرورگر باز می‌ماند؛ بین سناریوها بسته نمی‌شود.\n" +
          "برای OTP در بله کد را بفرست؛ این برگه را باز نگه دار.",
      );
      const out = await apiJson("/api/automation/playback/queue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const lines = [];
      for (const row of out.results || []) {
        lines.push(`=== ${row.label || row.script_id} | ${row.ok ? "انجام شد" : "نشد"} ===`);
        if (row.runs) {
          for (const r of row.runs) {
            if (r.ok && Array.isArray(r.log)) lines.push(...r.log);
            else if (r.error) lines.push(friendlyPlaybackFailureReason(r.error));
          }
        } else if (row.error) {
          lines.push(friendlyPlaybackFailureReason(row.error));
        }
      }
      if (out.queue_completed_ok) {
        lines.push("", "✓ همهٔ صف تمام شد؛ مرورگر بسته شد.");
        toast("صف با موفقیت تمام شد.", true);
      } else if (out.playback_cancelled) {
        lines.push("", "اجرا با توقف قطع شد.");
        toast("صف متوقف شد.", false);
      } else {
        lines.push("", "برخی سناریوها کامل نشدند.");
        toast("صف ناقص ماند؛ لاگ را ببین.", false);
      }
      if (out.playback_log_file) {
        lines.push("", `لاگ فنی: ${out.playback_log_file}`);
      }
      setQueueLog(lines.join("\n"));
      finishedClean = true;
    } catch (e) {
      const human = friendlyPlaybackFailureReason(String(e.message || e));
      toast(human, false, 10000);
      appendQueueLog(human);
    } finally {
      state.automation.playbackRunning = false;
      if (btnRun) btnRun.disabled = false;
      if (finishedClean) {
        state.automation.playbackCancelId = null;
        if (btnAbort) btnAbort.disabled = true;
      } else if (btnAbort) btnAbort.disabled = false;
    }
  });

  btnAbort?.addEventListener("click", async () => {
    const id = state.automation.playbackCancelId;
    if (!id) return;
    try {
      await fetch("/api/automation/playback/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cancellation_id: id }),
      });
      toast("توقف صف به سرور رسید.", true);
    } catch (_) {
      toast("ارسال توقف ناموفق بود.", false);
    }
  });
}

async function bindAutomationUI() {
  if (!el("btn_auto_open")) return;

  setAutoStatus("غیرفعال");

  el("btn_auto_open").addEventListener("click", async () => {
    const addr = String(el("auto_url").value || "").trim();
    if (!addr) {
      toast("آدرس شروع را بنویس (مثلاً https://example.com).", false, 9000);
      return;
    }
    toast(
      "در حال باز کردن Chromium… اگر بلافاصله ندیدی، چند ثانیه صبر کن؛ گاهی پنجره پشت بقیه باز می‌شود (Alt+Tab).",
      true,
      16000,
    );
    const ac = new AbortController();
    const tid = window.setTimeout(() => ac.abort(), 45000);
    try {
      const out = await apiJson("/api/automation/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: addr }),
        signal: ac.signal,
      });
      state.automation.sessionId = out.session_id;
      setAutoStatus(`مرورگر باز شد (session: ${out.session_id})`);
      setAutoLog(
        "پنجرهٔ مرورگر باید قابل دیدن باشد؛ اگر نیست Alt+Tab یا نوار وظیفه را نگاه کن.\n" +
          "بعد از دیدن صفحه، «شروع ضبط» را بزن و همان پنجره را استفاده کن.",
      );
      toast("اگر صفحه ندیدی، Alt+Tab بزن؛ جلسه همین‌جا در وضعیت نوشته شده.", true, 12000);
    } catch (e) {
      const m =
        e && /** @type {any} */ (e).name === "AbortError"
          ? "زمان انتظار تمام شد (۴۵ ثانیه). سرور پاسخ نداد؛ شاید Playwright/Chromium یا اینترنت نصب نشده باشد."
          : String(e.message || e);
      toast(`بازکردن ناموفق: ${m}`, false, 24000);
    } finally {
      window.clearTimeout(tid);
    }
  });

  el("btn_auto_rec_start").addEventListener("click", async () => {
    const sid = state.automation.sessionId;
    if (!sid) {
      toast("اول مرورگر را باز کن.", false);
      return;
    }
    try {
      await apiJson(`/api/automation/session/${sid}/record/start`, { method: "POST" });
      state.automation.recording = true;
      setAutoStatus("درحال ضبط");
      appendAutoLog("ضبط شروع شد.");
    } catch (e) {
      toast(`خطا: ${e.message || e}`, false);
    }
  });

  el("btn_auto_rec_stop").addEventListener("click", async () => {
    const sid = state.automation.sessionId;
    if (!sid) return;
    try {
      await apiJson(`/api/automation/session/${sid}/record/stop`, { method: "POST" });
      state.automation.recording = false;
      setAutoStatus("ضبط متوقف شد");
      appendAutoLog("ضبط متوقف شد.");
    } catch (e) {
      toast(`خطا: ${e.message || e}`, false);
    }
  });

  el("btn_auto_build").addEventListener("click", async () => {
    const sid = state.automation.sessionId;
    if (!sid) {
      toast("اول مرورگر را باز کن.", false);
      return;
    }
    try {
      const out = await apiJson(`/api/automation/session/${sid}/scenario`, { method: "POST" });
      setScenarioToEditor(out.scenario);
      appendAutoLog(`سناریو ساخته شد. تعداد step: ${(out.scenario.steps || []).length}`);
      toast("سناریو از ضبط ساخته شد.", true);
    } catch (e) {
      toast(`خطا: ${e.message || e}`, false);
    }
  });

  el("btn_auto_close").addEventListener("click", async () => {
    const sid = state.automation.sessionId;
    if (!sid) return;
    try {
      await apiJson(`/api/automation/session/${sid}/close`, { method: "POST" });
      state.automation.sessionId = null;
      state.automation.recording = false;
      setAutoStatus("مرورگر بسته شد");
      appendAutoLog("جلسه بسته شد.");
    } catch (e) {
      toast(`خطا: ${e.message || e}`, false);
    }
  });

  el("btn_auto_save").addEventListener("click", async () => {
    try {
      const scenario = getScenarioFromEditor();
      el("auto_scenario").value = JSON.stringify(scenario, null, 2);
      const out = await apiJson("/api/automation/scripts/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: el("auto_script_id").value.trim(),
          name: el("auto_script_name").value.trim(),
          scenario,
        }),
      });
      el("auto_script_id").value = out.id;
      if (!el("auto_script_name").value.trim()) el("auto_script_name").value = out.name;
      await reloadScriptsList();
      toast("سناریو ذخیره شد.", true);
    } catch (e) {
      toast(`ذخیره ناموفق: ${e.message || e}`, false);
    }
  });

  el("btn_auto_load").addEventListener("click", async () => {
    const fid = el("auto_scripts_list").value;
    if (!fid) return;
    try {
      const out = await apiJson(`/api/automation/scripts/${fid}`);
      setScenarioToEditor(out.script);
      const meta = out.script.meta || {};
      el("auto_script_id").value = meta.id || fid;
      el("auto_script_name").value = meta.name || "";
      toast("سناریو بارگذاری شد.", true);
    } catch (e) {
      toast(`بارگذاری ناموفق: ${e.message || e}`, false);
    }
  });

  el("btn_auto_delete").addEventListener("click", async () => {
    const fid = el("auto_scripts_list").value;
    if (!fid) return;
    try {
      await apiJson(`/api/automation/scripts/${fid}`, { method: "DELETE" });
      await reloadScriptsList();
      toast("سناریو حذف شد.", true);
    } catch (e) {
      toast(`حذف ناموفق: ${e.message || e}`, false);
    }
  });

  const btnAbortPlayback = el("btn_auto_abort");
  if (btnAbortPlayback) {
    btnAbortPlayback.addEventListener("click", async () => {
      const id = state.automation.playbackCancelId;
      if (!id) return;
      try {
        await fetch("/api/automation/playback/cancel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cancellation_id: id }),
        });
        toast("توقف به سرور رسید؛ حداکثر تا پایان همان بازهٔ ۱۰ ثانیه‌ای قطع می‌شود.", true, 6500);
      } catch (_) {
        toast("ارسال توقف ناموفق بود؛ همین اینترنت یا صفحه را چک کن.", false);
      }
    });
  }

  const hInp = el("auto_health_refresh_sec");
  const b30 = el("btn_health_refresh_30");
  const b60 = el("btn_health_refresh_60");
  const bOff = el("btn_health_refresh_off");
  if (b30 && hInp) b30.addEventListener("click", () => { hInp.value = "30"; });
  if (b60 && hInp) b60.addEventListener("click", () => { hInp.value = "60"; });
  if (bOff && hInp) bOff.addEventListener("click", () => { hInp.value = "0"; });

  const turboToggle = el("auto_turbo_mode");
  if (turboToggle) {
    turboToggle.addEventListener("change", () => {
      const pauseEl = el("auto_pause_steps_sec");
      if (!pauseEl) return;
      if (turboToggle.checked) {
        pauseEl.value = "0";
        pauseEl.disabled = true;
      } else {
        pauseEl.disabled = false;
      }
    });
  }

  el("btn_auto_run").addEventListener("click", async () => {
    const btnRun = el("btn_auto_run");
    const abortBtn = el("btn_auto_abort");

    if (state.automation.playbackRunning) {
      toast(
        "یک اجرا هنوز باز است؛ اگر گیر کرده «توقف اجرا» را بزن، یا تا جواب تمامٔ این درخواست صبر کن (بستن پیام خطا به‌معنای پایان اجرای سرور نیست).",
        false,
        11000,
      );
      return;
    }

    let cancelArmed = false;
    let finishedClean = false;
    try {
      state.automation.playbackRunning = true;
      const scenario = getScenarioFromEditor();
      const mk = el("auto_show_marker");
      if (mk) scenario.show_click_marker = mk.checked;
      const runs = getRunsFromEditor();
      const playbackDedicated = String(el("auto_playback_url").value || "").trim();
      const recordUrl = String(el("auto_url").value || "").trim();
      let fromScenario = "";
      const suMeta = scenario && scenario.start_url;
      if (suMeta != null && suMeta !== "") fromScenario = String(suMeta).trim();
      const startUrl = playbackDedicated || recordUrl || fromScenario || undefined;
      const isTurbo = !!el("auto_turbo_mode")?.checked;
      const pauseSec = isTurbo ? 0 : parseFloat(String(el("auto_pause_steps_sec")?.value || "0"));
      const payload = { scenario, runs, cancellation_id: uuid() };
      if (startUrl) payload.start_url = startUrl;
      if (Number.isFinite(pauseSec) && pauseSec >= 0) {
        payload.pause_between_steps_ms = Math.round(pauseSec * 1000);
      }
      const refreshSec = parseFloat(String(el("auto_health_refresh_sec")?.value || "30"));
      if (Number.isFinite(refreshSec) && refreshSec >= 0) {
        payload.playback_health_refresh_interval_s = refreshSec;
      }

      payload.turbo_mode = isTurbo;
      const refreshMsVal = parseInt(String(el("auto_refresh_ms")?.value || "80"), 10);
      payload.refresh_interval_ms = Number.isFinite(refreshMsVal) ? refreshMsVal : 80;
      const keepOpenVal = !!el("auto_keep_open")?.checked;
      payload.keep_open_on_missing = keepOpenVal;
      payload.keep_open = keepOpenVal;

      scenario.turbo_mode = isTurbo;
      scenario.refresh_interval_ms = payload.refresh_interval_ms;
      scenario.keep_open = keepOpenVal;
      scenario.keep_open_on_missing = keepOpenVal;
      scenario.keep_browser_on_failure = keepOpenVal;
      if (isTurbo) {
        scenario.pause_between_steps_ms = 0;
      }
      const notifyEl = el("auto_notify_bale");
      payload.notify_bale = !(notifyEl && !notifyEl.checked);
      payload.bale_bot_token = el("bale_bot_token").value.trim();
      payload.bale_chat_id = el("bale_chat_id").value.trim();
      payload.scenario_display_name = String(el("auto_script_name").value || "").trim();
      state.automation.playbackCancelId = payload.cancellation_id;
      cancelArmed = true;

      setAutoLog(
        "اجرا شروع شد. اگر سایت قطع است، در لاگ پایین هر چند ده ثانیه باید خطی مثل «بار n… صبر و دوباره» ببینی؛ اگر فقط یک خطا دیدی و تمام شد، متن را کپی کن و بگو.\n" +
          "این برگه را همان‌جا باز نگه دار؛ «توقف اجرا» برای قطع است.",
      );
      if (abortBtn) abortBtn.disabled = false;
      if (btnRun) btnRun.disabled = true;

      const out = await apiJson("/api/automation/playback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      const rows = Array.isArray(out.results) ? out.results : [];
      const playbackCompletedOk = out.playback_completed_ok === true;
      const closeFailed = out.playback_browser_close_failed === true;
      const rowsAllOk = rows.length > 0 && rows.every((r) => !!r.ok);
      const treatAsFullSuccess =
        playbackCompletedOk || (!out.playback_cancelled && rowsAllOk && rows.length > 0);
      const lines = [];
      if (out.playback_bale_otp_config_missing) {
        lines.push(
          "⚠ کد پیامک از بله فعال نیست: توکن بازو یا Chat ID در بخشٔ ۲ خالی است؛ هر دو را پر کن، «ذخیرهٔ همه چیز روی دیسک» بزن و دوباره اجرا کن.",
          "",
        );
      }
      for (const row of rows) {
        const ok = !!row.ok;
        lines.push(`=== ${row.label} | ${ok ? "انجام شد" : "انجام نشد"} ===`);
        if (ok && Array.isArray(row.log)) lines.push(...row.log);
        if (!ok && row.cancelled) {
          lines.push('با دکمهٔ «توقف اجرا» اجرا قطع شده است؛ در وقفهٔ بین هر تلاش هم بررسی می‌شود.');
        } else if (!ok && row.error) {
          lines.push(friendlyPlaybackFailureReason(row.error));
        }
      }
      if (out.playback_bale_end_skipped) {
        lines.push("", String(out.playback_bale_end_skipped));
      }
      if (out.playback_bale_report_skipped) {
        lines.push("", String(out.playback_bale_report_skipped));
      }
      if (out.playback_bale_connect_sent) {
        lines.push("", "به بله فرستاده شد: پیامٔ «شروع اجرای خودکار» (مرورگر در حال انجام سناریو است).");
      }
      if (out.playback_bale_finished_sent) {
        lines.push("", "به بله فرستاده شد: سناریو با موفقیت تمام شد.");
      }
      if (out.playback_bale_cancel_sent) {
        lines.push("", "به بله فرستاده شد: اجرا با توقف شما قطع شد.");
      }
      if (out.playback_bale_report_sent) {
        lines.push("", "به بله فرستاده شد: خلاصهٔ وضعیت برای اجرای ناقص یا خطادار.");
      }
      if (treatAsFullSuccess) {
        lines.push(
          "",
          closeFailed
            ? "⚠ کار انجام شد؛ سرور قطعی مرورگر را نبست (اگر پنجرهٔ کرومیوم مانده دستی ببند)."
            : "✓ کار انجام شد؛ پنجرهٔ اتوماسیون بسته شد.",
        );
      }
      if (out.playback_bale_start_error) {
        lines.push("", `پیامٔ شروع به بله نرسید: ${friendlyPlaybackFailureReason(out.playback_bale_start_error)}`);
      }
      if (out.playback_bale_report_error) {
        lines.push("", `خلاصه به بله نرسید: ${friendlyPlaybackFailureReason(out.playback_bale_report_error)}`);
      }
      if (out.playback_log_file) {
        lines.push("", `فایلٔ لاگ فنی پخش (برای اشکال‌زدایی): ${out.playback_log_file}`);
      }
      if (out.playback_survived_client_disconnect) {
        lines.push(
          "",
          "اتصال مرورگر با سرور یک لحظه قطع شد؛ اجرای پخش در پس‌زمینه کامل شد (تب دستیار را در حین انتظار باز نگه دار).",
        );
      }
      if (out.playback_note) {
        lines.push("", String(out.playback_note));
      }

      const anyFail = rows.some((r) => !r.ok);
      const cancelled = !!out.playback_cancelled;
      if (!rows.length) toast("نتیجه‌ای از اجرا برنگشت.", false);
      else if (treatAsFullSuccess) {
        toast(
          closeFailed ? "کار انجام شد؛ اگر مرورگر اتوماسیون باز ماند خودت ببند." : "کار انجام شد؛ مرورگر بسته شد.",
          true,
          8400,
        );
      } else if (cancelled) toast("اجرا با توقف شما متوقف شد.", false);
      else if (anyFail) toast("اجرای بعضی کارها کامل نشد؛ متن بالا را ببین.", false);

      finishedClean = true;
    } catch (e) {
      const raw = e && /** @type {any} */ (e).message != null ? String(/** @type {any} */ (e).message) : String(e);
      const human = friendlyPlaybackFailureReason(raw);
      toast(
        `${human} — اگر اجرای پشت‌زمینه هنوز دارد تلاش می‌کند، «توقف اجرا» را بزن (دکمه باید روشن مانده باشد).`,
        false,
        12000,
      );
      appendAutoLog(`قطع شدن یا خطای ارتباط با سرور؛ لزوماً پایان اجرای واقعی نیست؛ در صورت نیاز توقف بزن:\n${human}`);
    } finally {
      state.automation.playbackRunning = false;
      if (btnRun) btnRun.disabled = false;

      if (finishedClean && cancelArmed) {
        state.automation.playbackCancelId = null;
        if (abortBtn) abortBtn.disabled = true;
      } else if (cancelArmed && state.automation.playbackCancelId) {
        if (abortBtn) abortBtn.disabled = false;
      } else if (abortBtn) {
        abortBtn.disabled = true;
      }
    }
  });

  reloadScriptsList().catch((err) => {
    console.warn("reloadScriptsList", err);
  });
}

function bootstrap() {
  bindQueueUI();

  const btnSave = el("btn_save");
  if (btnSave) btnSave.addEventListener("click", () => persistConfigFromForm());

  const btnReload = el("btn_reload");
  if (btnReload) {
    btnReload.addEventListener("click", () =>
      loadConfigFromServer().then(() => toast("از دیسک خوانده شد.", true)).catch((e) => toast(String(e), false)),
    );
  }

  const btnTestBale = el("btn_test_bale");
  if (btnTestBale) btnTestBale.addEventListener("click", () => testBale());

  bindImportExport();
  bindAutomationUI().catch((e) => toast(`automation init: ${e.message || e}`, false));

  loadConfigFromServer().catch((e) => toast(String(e), false));
}

document.addEventListener("DOMContentLoaded", bootstrap);
