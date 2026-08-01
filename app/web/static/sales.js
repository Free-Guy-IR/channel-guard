(() => {
  "use strict";

  const BASE = window.ADMIN_BASE || "";
  document.getElementById("dash-link").href = `${BASE}/`;
  document.getElementById("nodes-link").href = `${BASE}/nodes`;
  document.getElementById("surveys-link").href = `${BASE}/surveys`;
  document.getElementById("sales-link").href = `${BASE}/sales`;
  document.getElementById("sales-users-link").href = `${BASE}/sales-users`;
  document.getElementById("backup-link").href = `${BASE}/backup`;

  // ---------- channel selector ----------
  function getSelectedChannel() {
    return localStorage.getItem("cg_channel") || "";
  }
  function setSelectedChannel(id) {
    localStorage.setItem("cg_channel", id);
  }

  async function loadChannels() {
    const resp = await fetch(`${BASE}/api/channels`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data.status) return;

    if (!getSelectedChannel()) setSelectedChannel(data.obj.primary_id);
    const current = getSelectedChannel();
    const select = document.getElementById("channel-select");
    select.innerHTML = data.obj.channels.map((c) =>
      `<option value="${c.id}"${c.id === current ? " selected" : ""}>${c.name}</option>`
    ).join("") + `<option value="__add__">+ افزودن کانال و ربات فروش جدید</option>`;

    window._pendingChannels = data.obj.pending || [];
  }

  document.getElementById("channel-select").addEventListener("change", (e) => {
    if (e.target.value === "__add__") {
      e.target.value = getSelectedChannel();
      openAddChannelModal();
      return;
    }
    setSelectedChannel(e.target.value);
    location.reload();
  });

  function openAddChannelModal() {
    document.getElementById("ac-note").textContent = "";
    document.getElementById("ac-name").value = "";
    document.getElementById("ac-id").value = "";
    document.getElementById("ac-channel-id").value = "";
    document.getElementById("ac-base-url").value = "";
    document.getElementById("ac-token").value = "";
    const pendingSelect = document.getElementById("ac-pending");
    const pending = window._pendingChannels || [];
    pendingSelect.innerHTML = `<option value="">— اگه از لیست شناسایی‌شده‌ها هست، انتخابش کن —</option>` +
      pending.map((p) => `<option value="${p.chat_id}">${p.title || "بدون‌اسم"} (${p.chat_id})</option>`).join("");
    document.getElementById("add-channel-modal-overlay").classList.add("open");
  }

  document.getElementById("ac-pending").addEventListener("change", (e) => {
    if (e.target.value) document.getElementById("ac-channel-id").value = e.target.value;
  });
  document.getElementById("ac-close-btn").addEventListener("click", () => {
    document.getElementById("add-channel-modal-overlay").classList.remove("open");
  });
  document.getElementById("ac-submit-btn").addEventListener("click", async () => {
    const note = document.getElementById("ac-note");
    const payload = {
      id: document.getElementById("ac-id").value.trim(),
      name: document.getElementById("ac-name").value.trim(),
      channel_id: document.getElementById("ac-channel-id").value.trim(),
      sales_api_base_url: document.getElementById("ac-base-url").value.trim(),
      sales_api_token: document.getElementById("ac-token").value.trim(),
    };
    note.textContent = "در حال ثبت...";
    const resp = await fetch(`${BASE}/api/channels`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!data.status) { note.textContent = data.msg || "خطا در ثبت"; return; }
    note.textContent = data.obj.msg;
    loadChannels();
  });

  const TZ = "Asia/Tehran";
  const fmtNum = new Intl.NumberFormat("en-US");
  const jalaliShort = new Intl.DateTimeFormat("fa-IR-u-ca-persian", { timeZone: TZ, month: "short", day: "2-digit" });
  const jalaliDateOnly = new Intl.DateTimeFormat("fa-IR-u-ca-persian", {
    timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
  });
  const jalaliDateTime = new Intl.DateTimeFormat("fa-IR-u-ca-persian", {
    timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const jalaliMonthYear = new Intl.DateTimeFormat("fa-IR-u-ca-persian", { timeZone: TZ, year: "numeric", month: "long" });

  const GRANULARITY_LABELS = { day: "روزانه", week: "هفتگی", month: "ماهانه" };

  function copyable(value) {
    if (!value) return "-";
    return `<span class="copyable" data-copy="${value}">${value}</span>`;
  }

  async function copyText(text) {
    if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
      try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* fall through */ }
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  function attachCopyHandlers(root) {
    root.querySelectorAll(".copyable").forEach((el) => {
      el.addEventListener("click", async () => {
        const ok = await copyText(el.dataset.copy);
        if (ok) {
          el.classList.add("copied");
          setTimeout(() => el.classList.remove("copied"), 900);
        }
      });
    });
  }

  // fixed categorical order, never cycled/reassigned per-render
  const PANEL_COLORS = ["#3b5bfd", "#8256e0", "#d98d1b", "#14b8a6", "#e2495c", "#1fa159", "#ec4899", "#64748b"];

  let windowDays = 30;
  let granularity = "day";
  let trendChart = null;
  let panelChart = null;

  // Buckets come back from the server as plain grouping keys, not real
  // calendar dates for week/month ("2026-W29", "2026-07") - handing those
  // straight to Chart.js as labels made it try to auto-detect a time scale
  // and mangle them ("W29 2 28 2026"). Turn each into a clean, unambiguous
  // display string here, and the x-axis is forced to a category scale
  // below so Chart.js never attempts that guess again.
  function fmtBucketLabel(bucket) {
    if (granularity === "day") {
      return jalaliShort.format(new Date(bucket + "T12:00:00Z"));
    }
    if (granularity === "week") {
      const m = bucket.match(/^(\d{4})-W(\d{1,2})$/);
      return m ? `هفته ${fmtNum.format(parseInt(m[2], 10))}` : bucket;
    }
    if (granularity === "month") {
      const m = bucket.match(/^(\d{4})-(\d{2})$/);
      return m ? jalaliMonthYear.format(new Date(`${bucket}-01T12:00:00Z`)) : bucket;
    }
    return bucket;
  }

  function fmtToman(n) {
    return fmtNum.format(Math.round(n || 0));
  }

  // windowStart (epoch seconds), when given, is the server's authoritative
  // boundary - needed because "امروز" isn't a rolling 24h window, it's
  // calendar-day-aligned to Tehran midnight (see analytics.py). Without an
  // arg yet (e.g. right after clicking, before the fetch resolves) this
  // falls back to the same rolling estimate used for every other window,
  // just for instant visual feedback.
  function renderRangeNote(windowStart) {
    const now = new Date();
    const start = windowStart != null ? new Date(windowStart * 1000) : new Date(now.getTime() - windowDays * 86400 * 1000);
    document.getElementById("range-note").textContent =
      `بازه‌ی انتخاب‌شده: از ${jalaliDateOnly.format(start)} تا ${jalaliDateOnly.format(now)}`;
  }

  document.getElementById("window-picker").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-days]");
    if (!btn) return;
    document.querySelectorAll("#window-picker button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    windowDays = parseInt(btn.dataset.days, 10);
    renderRangeNote();
    load();
  });

  document.getElementById("granularity-picker").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-g]");
    if (!btn) return;
    document.querySelectorAll("#granularity-picker button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    granularity = btn.dataset.g;
    load();
  });

  async function load() {
    const resp = await fetch(`${BASE}/api/sales?window_days=${windowDays}&granularity=${granularity}&channel=${encodeURIComponent(getSelectedChannel())}`);
    if (resp.status === 401) { location.href = `${BASE}/`; return; }
    const data = await resp.json();
    if (!data.status) return;
    render(data.obj);
  }

  function render(obj) {
    renderRangeNote(obj.window_start);
    document.getElementById("stat-count").textContent = fmtNum.format(obj.total_count);
    document.getElementById("stat-revenue").textContent = fmtToman(obj.total_revenue);
    document.getElementById("stat-avg").textContent = fmtToman(obj.avg_price);
    document.getElementById("stat-top-panel").textContent =
      obj.top_panels.length ? obj.top_panels[0].panel_name : "-";

    const fc = obj.forecast_30d || {};
    const fcEl = document.getElementById("stat-forecast");
    const fcHintEl = document.getElementById("stat-forecast-hint");
    if (fc.available) {
      fcEl.textContent = `${fmtToman(fc.total_revenue)} تومان`;
      fcHintEl.textContent = `میانگین ${fmtToman(fc.avg_daily_revenue)} تومان/روز · بر اساس ${fmtNum.format(fc.training_days)} روز اخیر`;
    } else {
      fcEl.textContent = "-";
      fcHintEl.textContent = fc.reason || "";
    }

    const windowLabel = { 7: "۷", 30: "۳۰", 90: "۹۰", 180: "۱۸۰" }[obj.window_days] || obj.window_days;
    document.getElementById("trend-hint").textContent =
      `${windowLabel} روز اخیر · ${GRANULARITY_LABELS[obj.granularity]} · تعداد فروش هم روی هاور نشون داده می‌شه`;

    const syncNote = document.getElementById("sync-note");
    if (obj.synced_invoices) {
      const when = obj.last_synced_at ? jalaliDateTime.format(new Date(obj.last_synced_at * 1000)) : "-";
      syncNote.textContent = `${fmtNum.format(obj.synced_invoices)} فاکتور همگام‌سازی‌شده · آخرین همگام‌سازی: ${when}`;
    } else {
      syncNote.textContent = "هنوز هیچ فاکتوری همگام‌سازی نشده - چند دقیقه صبر کن تا اولین همگام‌سازی کامل بشه";
    }

    renderTrendChart(obj.series);
    renderPanelBars(obj.top_panels, obj.total_count);
    renderPanelsTable(obj.top_panels);
    renderPanelChart(obj.top_panels, obj.panel_series);
  }

  function renderTrendChart(series) {
    const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    const labels = series.map((r) => fmtBucketLabel(r.bucket));
    const revenue = series.map((r) => r.revenue || 0);
    const counts = series.map((r) => r.count || 0);

    const ctx = document.getElementById("salesTrendChart");
    const config = {
      type: "bar",
      data: {
        labels,
        datasets: [{
          label: "درآمد",
          data: revenue,
          backgroundColor: cssVar("--primary") + "cc",
          borderRadius: 4,
          maxBarThickness: 28,
        }],
      },
      options: {
        responsive: true,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: cssVar("--tooltip-bg"), borderColor: cssVar("--card-border"), borderWidth: 1,
            titleColor: cssVar("--text-primary"), bodyColor: cssVar("--text-secondary"), padding: 10, cornerRadius: 10,
            callbacks: {
              label: (item) => {
                const i = item.dataIndex;
                return [`درآمد: ${fmtToman(revenue[i])} تومان`, `تعداد فروش: ${fmtNum.format(counts[i])}`];
              },
            },
          },
        },
        scales: {
          x: { type: "category", grid: { display: false }, ticks: { color: cssVar("--text-muted"), font: { size: 11 }, maxRotation: 0, autoSkip: true } },
          y: { beginAtZero: true, grid: { color: cssVar("--gridline") }, ticks: { color: cssVar("--text-muted"), font: { size: 11 } } },
        },
      },
    };

    if (trendChart) { trendChart.data = config.data; trendChart.options = config.options; trendChart.update(); }
    else trendChart = new Chart(ctx, config);
  }

  function renderPanelBars(topPanels, totalCount) {
    const box = document.getElementById("panel-bars");
    if (!topPanels.length) { box.innerHTML = `<div class="empty-hint">داده‌ای نیست</div>`; return; }
    const maxCount = Math.max(...topPanels.map((p) => p.count));
    box.innerHTML = topPanels.map((p, i) => `
      <div class="panel-bar-row">
        <span class="panel-bar-name">${escapeHtml(p.panel_name)}</span>
        <span class="panel-bar-track"><span class="panel-bar-fill" style="width:${(p.count / maxCount * 100).toFixed(1)}%; background:${PANEL_COLORS[i % PANEL_COLORS.length]};"></span></span>
        <span class="panel-bar-count">${fmtNum.format(p.count)}</span>
      </div>`).join("");
  }

  function renderPanelsTable(topPanels) {
    const body = document.getElementById("panels-body");
    if (!topPanels.length) {
      body.innerHTML = `<tr><td colspan="3" class="empty-hint">داده‌ای نیست</td></tr>`;
      return;
    }
    body.innerHTML = topPanels.map((p) => `
      <tr>
        <td>${escapeHtml(p.panel_name)}</td>
        <td class="num">${fmtNum.format(p.count)}</td>
        <td class="num">${fmtToman(p.revenue)}</td>
      </tr>`).join("");
  }

  function renderPanelChart(topPanels, panelSeries) {
    const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    const allBuckets = new Set();
    topPanels.forEach((p) => (panelSeries[p.panel_name] || []).forEach((r) => allBuckets.add(r.bucket)));
    const buckets = Array.from(allBuckets).sort();
    const labels = buckets.map((b) => fmtBucketLabel(b));

    const datasets = topPanels.map((p, i) => {
      const rows = panelSeries[p.panel_name] || [];
      const byBucket = Object.fromEntries(rows.map((r) => [r.bucket, r.count]));
      const color = PANEL_COLORS[i % PANEL_COLORS.length];
      return {
        label: p.panel_name,
        data: buckets.map((b) => byBucket[b] || 0),
        borderColor: color,
        backgroundColor: color,
        borderWidth: 2,
        pointRadius: 2,
        pointHoverRadius: 4,
        pointBackgroundColor: color,
        pointBorderColor: cssVar("--ring"),
        pointBorderWidth: 1,
        tension: 0.25,
        fill: false,
      };
    });

    const ctx = document.getElementById("panelChart");
    const config = {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            position: "bottom",
            labels: { color: cssVar("--text-secondary"), font: { size: 10.5 }, boxWidth: 10, padding: 10 },
          },
          tooltip: {
            backgroundColor: cssVar("--tooltip-bg"), borderColor: cssVar("--card-border"), borderWidth: 1,
            titleColor: cssVar("--text-primary"), bodyColor: cssVar("--text-secondary"), padding: 10, cornerRadius: 10,
          },
        },
        scales: {
          x: { type: "category", grid: { display: false }, ticks: { color: cssVar("--text-muted"), font: { size: 10 }, maxRotation: 0, autoSkip: true } },
          y: { beginAtZero: true, grid: { color: cssVar("--gridline") }, ticks: { color: cssVar("--text-muted"), font: { size: 10 }, precision: 0 } },
        },
      },
    };

    if (panelChart) { panelChart.data = config.data; panelChart.options = config.options; panelChart.update(); }
    else panelChart = new Chart(ctx, config);
  }

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s == null ? "" : s;
    return div.innerHTML;
  }

  // ---------- unverified (manually-checked) payments modal ----------
  const PAY_STATUS_LABELS = { paid: "✅ تایید شده", reject: "❌ رد شده", expire: "⏳ تایید نشده (منقضی)" };

  function payStatusChip(status) {
    const cls = ["paid", "reject", "expire"].includes(status) ? status : "other";
    const label = PAY_STATUS_LABELS[status] || status || "-";
    return `<span class="pay-status-chip ${cls}">${label}</span>`;
  }

  function renderUnverifiedTable(payments) {
    const body = document.getElementById("unverified-body");
    if (!payments.length) {
      body.innerHTML = `<tr><td colspan="5" class="empty-hint">موردی پیدا نشد</td></tr>`;
      return;
    }
    body.innerHTML = payments.map((p) => `
      <tr>
        <td>${copyable(p.id)}</td>
        <td>${copyable(p.chat_id)}</td>
        <td class="num">${fmtNum.format(p.price)} تومان</td>
        <td class="num">${jalaliDateTime.format(new Date(p.payment_time * 1000))}</td>
        <td>${payStatusChip(p.payment_status)}</td>
      </tr>`).join("");
    attachCopyHandlers(body);
  }

  async function loadUnverifiedPayments() {
    const body = document.getElementById("unverified-body");
    body.innerHTML = `<tr><td colspan="5" class="empty-hint">در حال بارگذاری...</td></tr>`;
    const resp = await fetch(`${BASE}/api/sales/unverified-payments?channel=${encodeURIComponent(getSelectedChannel())}`);
    if (resp.status === 401) { location.href = `${BASE}/`; return; }
    const data = await resp.json();
    if (!data.status) return;

    const when = data.obj.last_synced_at ? jalaliDateTime.format(new Date(data.obj.last_synced_at * 1000)) : "-";
    document.getElementById("unverified-sync-note").textContent =
      `${fmtNum.format(data.obj.payments.length)} مورد در انتظار · آخرین همگام‌سازی: ${when}`;

    renderUnverifiedTable(data.obj.payments);
  }

  document.getElementById("open-unverified-btn").addEventListener("click", () => {
    document.getElementById("unverified-modal-overlay").classList.add("open");
    loadUnverifiedPayments();
  });
  document.getElementById("unverified-close-btn").addEventListener("click", () => {
    document.getElementById("unverified-modal-overlay").classList.remove("open");
  });

  loadChannels();
  renderRangeNote();
  load();
  setInterval(load, 60000);
})();
