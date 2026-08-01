(() => {
  "use strict";

  const BASE = window.ADMIN_BASE || "";
  document.getElementById("dash-link").href = `${BASE}/`;
  document.getElementById("nodes-link").href = `${BASE}/nodes`;
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
      admin_bot_token: document.getElementById("ac-admin-bot-token").value.trim(),
      admin_chat_id: document.getElementById("ac-admin-chat-id").value.trim(),
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

  // ---------- per-channel settings modal ----------
  async function openChannelSettingsModal() {
    const note = document.getElementById("cs-note");
    note.textContent = "در حال بارگذاری...";
    document.getElementById("channel-settings-modal-overlay").classList.add("open");
    const chId = getSelectedChannel();
    const resp = await fetch(`${BASE}/api/channels/${encodeURIComponent(chId)}`);
    const data = await resp.json();
    if (!data.status) { note.textContent = data.msg || "خطا در بارگذاری"; return; }
    const c = data.obj;
    document.getElementById("cs-name").value = c.name;
    document.getElementById("cs-channel-id").value = c.channel_id;
    document.getElementById("cs-base-url").value = c.sales_api_base_url;
    document.getElementById("cs-token").value = c.sales_api_token;
    document.getElementById("cs-admin-bot-token").value = c.admin_bot_token;
    document.getElementById("cs-admin-chat-id").value = c.admin_chat_id;
    note.textContent = "";
  }

  document.getElementById("channel-settings-btn").addEventListener("click", openChannelSettingsModal);
  document.getElementById("cs-close-btn").addEventListener("click", () => {
    document.getElementById("channel-settings-modal-overlay").classList.remove("open");
  });
  document.getElementById("cs-submit-btn").addEventListener("click", async () => {
    const note = document.getElementById("cs-note");
    const chId = getSelectedChannel();
    const payload = {
      name: document.getElementById("cs-name").value.trim(),
      channel_id: document.getElementById("cs-channel-id").value.trim(),
      sales_api_base_url: document.getElementById("cs-base-url").value.trim(),
      sales_api_token: document.getElementById("cs-token").value.trim(),
      admin_bot_token: document.getElementById("cs-admin-bot-token").value.trim(),
      admin_chat_id: document.getElementById("cs-admin-chat-id").value.trim(),
    };
    note.textContent = "در حال ذخیره...";
    const resp = await fetch(`${BASE}/api/channels/${encodeURIComponent(chId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!data.status) { note.textContent = data.msg || "خطا در ذخیره"; return; }
    note.textContent = data.obj.msg;
    loadChannels();
  });

  const TZ = "Asia/Tehran";
  const fmtNum = new Intl.NumberFormat("en-US");

  const jalaliDateTime = new Intl.DateTimeFormat("fa-IR-u-ca-persian", {
    timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const jalaliShort = new Intl.DateTimeFormat("fa-IR-u-ca-persian", {
    timeZone: TZ, month: "short", day: "2-digit",
  });

  const fmtDate = (ts) => {
    if (!ts) return "-";
    return jalaliDateTime.format(new Date(ts * 1000));
  };
  const fmtDayShort = (ts) => jalaliShort.format(new Date(ts * 1000));

  function testChip(limitUsertest, found) {
    if (!found && found !== false) return `<span class="test-chip none">-</span>`;
    if (limitUsertest == null) return `<span class="test-chip none">-</span>`;
    if (limitUsertest < 0) return `<span class="test-chip">نامحدود</span>`;
    if (limitUsertest === 0) return `<span class="test-chip limited">${fmtNum.format(limitUsertest)}</span>`;
    return `<span class="test-chip">${fmtNum.format(limitUsertest)}</span>`;
  }

  let windowDays = 7;
  let chart = null;
  let ws = null;
  let stateReloadTimer = null;

  // ---------- login ----------
  const loginScreen = document.getElementById("login-screen");
  const appRoot = document.getElementById("app");
  const loginBtn = document.getElementById("login-btn");
  const loginPassword = document.getElementById("login-password");
  const loginError = document.getElementById("login-error");

  async function tryLogin() {
    loginError.textContent = "";
    const password = loginPassword.value;
    try {
      const resp = await fetch(`${BASE}/api/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (!resp.ok) {
        loginError.textContent = resp.status === 401
          ? "رمز عبور نادرست است"
          : `خطا در ارتباط با سرور (کد ${resp.status}) - صفحه رو hard refresh کن`;
        return;
      }
      showApp();
    } catch (e) {
      loginError.textContent = "خطا در ارتباط با سرور";
    }
  }

  loginBtn.addEventListener("click", tryLogin);
  loginPassword.addEventListener("keydown", (e) => { if (e.key === "Enter") tryLogin(); });

  function showApp() {
    loginScreen.style.display = "none";
    appRoot.style.display = "block";
    loadChannels();
    loadState();
    connectWS();
  }

  async function checkSession() {
    const resp = await fetch(`${BASE}/api/state`);
    if (resp.ok) {
      showApp();
    }
  }

  // ---------- window picker ----------
  function updateWindowLabels(text) {
    document.getElementById("chart-window-label").textContent = text;
    document.getElementById("jm-window-label").textContent = `${text} · چت‌آیدی و یوزرنیم قابل کپی`;
    document.getElementById("rm-window-label").textContent = `${text} · لفت داده و دوباره عضو شده`;
  }

  document.getElementById("window-picker").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-days]");
    if (!btn) return;
    document.querySelectorAll("#window-picker button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    windowDays = parseInt(btn.dataset.days, 10);
    updateWindowLabels(btn.textContent);
    loadState();
  });

  // ---------- data load ----------
  async function loadState() {
    const resp = await fetch(`${BASE}/api/state?window_days=${windowDays}&channel=${encodeURIComponent(getSelectedChannel())}`);
    if (resp.status === 401) {
      appRoot.style.display = "none";
      loginScreen.style.display = "flex";
      return;
    }
    const data = await resp.json();
    if (!data.status) return;
    render(data.obj);
  }

  function render(obj) {
    document.getElementById("channel-count").textContent =
      obj.channel_member_count != null ? fmtNum.format(obj.channel_member_count) : "-";

    document.getElementById("stat-joins").textContent = fmtNum.format(obj.overview.total_joins);
    document.getElementById("stat-leaves").textContent = fmtNum.format(obj.overview.total_leaves);

    const netEl = document.getElementById("stat-net");
    const net = obj.overview.net;
    netEl.textContent = (net > 0 ? "+" : "") + fmtNum.format(net);
    netEl.className = "value " + (net > 0 ? "join" : net < 0 ? "leave" : "");

    const rate = obj.joiners.purchase_rate_pct || 0;
    document.getElementById("stat-purchase-rate").textContent = rate.toFixed(1) + "%";
    document.getElementById("purchase-rate-fill").style.width = Math.min(rate, 100) + "%";

    const testedRate = obj.joiners.tested_rate_pct || 0;
    document.getElementById("stat-tested-rate").textContent = testedRate.toFixed(1) + "%";
    document.getElementById("tested-rate-fill").style.width = Math.min(testedRate, 100) + "%";
    document.getElementById("stat-tested-count").textContent =
      `${fmtNum.format(obj.joiners.tested_count)} از ${fmtNum.format(obj.joiners.count)} نفر`;

    const at = obj.all_tested_summary;
    const allTestedRate = at.tested_rate_pct || 0;
    document.getElementById("stat-all-tested-rate").textContent = allTestedRate.toFixed(1) + "%";
    document.getElementById("all-tested-rate-fill").style.width = Math.min(allTestedRate, 100) + "%";
    document.getElementById("stat-all-tested-count").textContent =
      `${fmtNum.format(at.tested_count)} از ${fmtNum.format(at.total)} نفر`;

    const js = obj.joined_summary;
    document.getElementById("jm-purchasers").textContent = `${fmtNum.format(js.purchasers_count)} از ${fmtNum.format(js.total)}`;
    document.getElementById("jm-avg-sum").textContent = js.avg_sum_payment != null ? fmtNum.format(Math.round(js.avg_sum_payment)) : "-";
    document.getElementById("jm-rate").textContent = js.purchase_rate_pct.toFixed(1) + "%";

    const rm = obj.returning_summary;
    document.getElementById("rm-count").textContent = `${fmtNum.format(rm.count)} از ${fmtNum.format(rm.total_joined)}`;
    document.getElementById("rm-rate").textContent = rm.rate_pct.toFixed(1) + "%";

    const rpRateEl = document.getElementById("stat-returning-purchase-rate");
    const rpCountEl = document.getElementById("stat-returning-purchase-count");
    if (rm.purchased_again_rate_pct == null) {
      rpRateEl.textContent = "-";
      rpCountEl.textContent = "هنوز داده‌ای نیست";
    } else {
      rpRateEl.textContent = rm.purchased_again_rate_pct.toFixed(1) + "%";
      rpCountEl.textContent = `${fmtNum.format(rm.purchased_again_count)} از ${fmtNum.format(rm.purchased_again_measurable)} نفر`;
    }

    renderJoinedTable(obj.joined_members);
    renderReturningMembers(rm.members);
    renderLeavers(obj.leavers);
    renderZeroPurchase(obj.joiners.zero_purchase);
    renderChart(obj.overview.daily);
  }

  function copyable(value) {
    return `<span class="copyable" data-copy="${value}">${value}</span>`;
  }

  function copyableUsername(username) {
    return username ? copyable(username) : "-";
  }

  // navigator.clipboard silently fails/is unavailable on non-HTTPS origins
  // (this panel runs on plain HTTP), so fall back to the legacy select+copy
  // approach whenever the modern API doesn't actually work.
  async function copyText(text) {
    if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (e) { /* fall through */ }
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

  function renderJoinedTable(members) {
    const body = document.getElementById("joined-body");
    if (!members.length) {
      body.innerHTML = `<tr><td colspan="5" class="empty-hint">هنوز موردی ثبت نشده</td></tr>`;
      return;
    }
    body.innerHTML = members.map((m) => `
      <tr>
        <td>${copyableUsername(m.username)}</td>
        <td>${copyable(m.chat_id)}</td>
        <td class="num">${fmtDate(m.first_seen_at)}</td>
        <td class="num">${m.found ? fmtNum.format(m.count_payment || 0) : "نامشخص"}</td>
        <td>${testChip(m.limit_usertest, m.found)}</td>
      </tr>`).join("");
    attachCopyHandlers(body);
  }

  function purchaseSplitCell(m) {
    if (m.count_payment_before == null) return fmtNum.format(m.current_count_payment || 0);
    return `${fmtNum.format(m.count_payment_before)} / ${fmtNum.format(m.count_payment_after)}`;
  }

  function renderReturningMembers(members) {
    const body = document.getElementById("returning-body");
    if (!members.length) {
      body.innerHTML = `<tr><td colspan="4" class="empty-hint">کاربر برگشتی‌ای در این بازه نیست</td></tr>`;
      return;
    }
    body.innerHTML = members.map((m) => `
      <tr>
        <td>${copyableUsername(m.username)}</td>
        <td>${copyable(m.chat_id)}</td>
        <td class="num">${fmtDate(m.last_joined_at)}</td>
        <td class="num">${purchaseSplitCell(m)}</td>
      </tr>`).join("");
    attachCopyHandlers(body);
  }

  function renderLeavers(leavers) {
    document.getElementById("lv-avg-join").textContent = fmtDate(leavers.avg_time_join ? Math.round(leavers.avg_time_join) : null);
    document.getElementById("lv-avg-count").textContent = leavers.avg_count_payment != null ? leavers.avg_count_payment.toFixed(1) : "-";
    document.getElementById("lv-avg-sum").textContent = leavers.avg_sum_payment != null ? fmtNum.format(Math.round(leavers.avg_sum_payment)) : "-";
    document.getElementById("lv-total-sum").textContent = fmtNum.format(leavers.total_sum_payment || 0);

    const body = document.getElementById("leavers-body");
    if (!leavers.recent.length) {
      body.innerHTML = `<tr><td colspan="6" class="empty-hint">لفتی در این بازه ثبت نشده</td></tr>`;
      return;
    }
    body.innerHTML = leavers.recent.map((r) => `
      <tr>
        <td>${copyableUsername(r.username)}</td>
        <td>${copyable(r.chat_id)}</td>
        <td class="num">${r.found ? fmtDate(r.time_join) : "نامشخص"}</td>
        <td class="num">${r.found ? fmtNum.format(r.count_payment) : "نامشخص"}</td>
        <td class="num">${r.found ? fmtNum.format(r.sum_payment) : "نامشخص"}</td>
        <td>${testChip(r.limit_usertest, r.found)}</td>
      </tr>`).join("");
    attachCopyHandlers(body);
  }

  function renderZeroPurchase(list) {
    const body = document.getElementById("zero-purchase-body");
    if (!list.length) {
      body.innerHTML = `<tr><td colspan="3" class="empty-hint">همه‌ی تازه‌واردها حداقل یک خرید داشته‌اند</td></tr>`;
      return;
    }
    body.innerHTML = list.map((r) => `
      <tr>
        <td>${copyableUsername(r.username)}</td>
        <td>${copyable(r.chat_id)}</td>
        <td class="num">${fmtDate(r.event_at)}</td>
      </tr>`).join("");
    attachCopyHandlers(body);
  }

  function renderChart(daily) {
    const dayKeys = [];
    const now = new Date();
    for (let i = windowDays - 1; i >= 0; i--) {
      const d = new Date(now.getTime() - i * 86400000);
      dayKeys.push(d.toISOString().slice(0, 10));
    }
    const byDay = Object.fromEntries(daily.map((r) => [r.day, r]));
    const joins = dayKeys.map((d) => (byDay[d] ? byDay[d].joins : 0));
    const leaves = dayKeys.map((d) => (byDay[d] ? byDay[d].leaves : 0));
    const purchasing = dayKeys.map((d) => (byDay[d] ? byDay[d].purchasing_joins : 0));
    const rejoins = dayKeys.map((d) => (byDay[d] ? byDay[d].rejoins : 0));
    const labels = dayKeys.map((d) => fmtDayShort(Date.parse(d + "T12:00:00Z") / 1000));

    const ctx = document.getElementById("trendChart");
    const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

    const config = {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "جوین",
            data: joins,
            borderColor: cssVar("--series-join"),
            backgroundColor: cssVar("--series-join") + "1a",
            borderWidth: 2,
            pointRadius: 3,
            pointHoverRadius: 5,
            pointBackgroundColor: cssVar("--series-join"),
            pointBorderColor: cssVar("--ring"),
            pointBorderWidth: 2,
            tension: 0.25,
            fill: true,
          },
          {
            label: "لفت",
            data: leaves,
            borderColor: cssVar("--series-leave"),
            backgroundColor: cssVar("--series-leave") + "1a",
            borderWidth: 2,
            borderDash: [6, 3],
            pointRadius: 3,
            pointHoverRadius: 5,
            pointStyle: "rectRot",
            pointBackgroundColor: cssVar("--series-leave"),
            pointBorderColor: cssVar("--ring"),
            pointBorderWidth: 2,
            tension: 0.25,
            fill: true,
          },
          {
            label: "خرید حداقل یک‌بار",
            data: purchasing,
            borderColor: cssVar("--series-purchase"),
            backgroundColor: "transparent",
            borderWidth: 2,
            borderDash: [2, 3],
            pointRadius: 3,
            pointHoverRadius: 5,
            pointStyle: "triangle",
            pointBackgroundColor: cssVar("--series-purchase"),
            pointBorderColor: cssVar("--ring"),
            pointBorderWidth: 2,
            tension: 0.25,
            fill: false,
          },
          {
            label: "اعضای برگشتی",
            data: rejoins,
            borderColor: cssVar("--series-rejoin"),
            backgroundColor: "transparent",
            borderWidth: 2,
            borderDash: [1, 3],
            pointRadius: 3,
            pointHoverRadius: 5,
            pointStyle: "star",
            pointBackgroundColor: cssVar("--series-rejoin"),
            pointBorderColor: cssVar("--ring"),
            pointBorderWidth: 2,
            tension: 0.25,
            fill: false,
          },
        ],
      },
      options: {
        responsive: true,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: cssVar("--tooltip-bg"),
            borderColor: cssVar("--card-border"),
            borderWidth: 1,
            titleColor: cssVar("--text-primary"),
            bodyColor: cssVar("--text-secondary"),
            padding: 10,
            cornerRadius: 10,
          },
        },
        scales: {
          x: {
            grid: { color: cssVar("--gridline"), drawTicks: false },
            ticks: { color: cssVar("--text-muted"), font: { size: 11 }, maxRotation: 0, autoSkip: true },
          },
          y: {
            beginAtZero: true,
            grid: { color: cssVar("--gridline"), drawTicks: false },
            ticks: { color: cssVar("--text-muted"), font: { size: 11 }, precision: 0 },
          },
        },
      },
    };

    if (chart) {
      chart.data = config.data;
      chart.update();
    } else {
      chart = new Chart(ctx, config);
    }
  }

  // ---------- websocket (live state refresh on join/leave) ----------
  function connectWS() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}${BASE}/ws`);
    const dot = document.getElementById("ws-dot");
    const label = document.getElementById("ws-label");

    ws.onopen = () => { dot.className = "dot on"; label.textContent = "اتصال زنده برقرار است"; };
    ws.onclose = () => {
      dot.className = "dot off"; label.textContent = "اتصال قطع شد - تلاش مجدد...";
      setTimeout(connectWS, 3000);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (msg) => {
      let evt;
      try { evt = JSON.parse(msg.data); } catch (e) { return; }
      if (evt.type !== "join" && evt.type !== "leave") return;
      clearTimeout(stateReloadTimer);
      stateReloadTimer = setTimeout(loadState, 700);
    };
  }

  checkSession();
})();
