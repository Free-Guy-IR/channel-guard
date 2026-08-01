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
  const jalaliDateTime = new Intl.DateTimeFormat("fa-IR-u-ca-persian", {
    timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });

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

  function testChip(limitUsertest) {
    if (limitUsertest == null) return `<span class="test-chip none">-</span>`;
    if (limitUsertest < 0) return `<span class="test-chip">نامحدود</span>`;
    if (limitUsertest === 0) return `<span class="test-chip limited">${fmtNum.format(limitUsertest)}</span>`;
    return `<span class="test-chip">${fmtNum.format(limitUsertest)}</span>`;
  }

  const PAY_STATUS_LABELS = {
    paid: "✅ تایید شده", reject: "❌ رد شده", expire: "⏳ تایید نشده (منقضی)", waiting: "🕓 در انتظار تایید",
  };

  function payStatusChip(status) {
    const cls = ["paid", "reject", "expire", "waiting"].includes(status) ? status : "other";
    const label = PAY_STATUS_LABELS[status] || status || "-";
    return `<span class="pay-status-chip ${cls}">${label}</span>`;
  }

  let currentFilter = "";
  let currentQuery = "";
  let currentPage = 1;
  let lastTotal = 0;
  let lastLimit = 50;
  let searchDebounce = null;
  let sortBy = null;
  let sortDir = "desc";

  function renderStats(stats) {
    const total = stats.total || 0;
    document.getElementById("stat-total").textContent = fmtNum.format(total);

    const setPctTile = (pctId, fillId, countId, count) => {
      const pct = total > 0 ? (count / total) * 100 : 0;
      document.getElementById(pctId).textContent = pct.toFixed(1) + "%";
      document.getElementById(fillId).style.width = Math.min(100, pct).toFixed(1) + "%";
      document.getElementById(countId).textContent = `${fmtNum.format(count)} کاربر`;
    };
    setPctTile("stat-np30", "np30-fill", "np30-count", stats.no_purchase_30 || 0);
    setPctTile("stat-np60", "np60-fill", "np60-count", stats.no_purchase_60 || 0);
    setPctTile("stat-tested30", "tested30-fill", "tested30-count", stats.tested_30 || 0);
  }

  function renderUsersTable(users) {
    const body = document.getElementById("users-body");
    if (!users.length) {
      body.innerHTML = `<tr><td colspan="9" class="empty-hint">موردی پیدا نشد</td></tr>`;
      return;
    }
    body.innerHTML = users.map((u) => `
      <tr>
        <td>${copyable(u.chat_id)}</td>
        <td>${u.username ? copyable("@" + u.username) : "-"}</td>
        <td class="num">${fmtNum.format(u.purchase_count)}</td>
        <td class="num">${fmtNum.format(u.deposit_count)}</td>
        <td class="num">${u.unpaid_count > 0 ? `<span class="test-chip limited">${fmtNum.format(u.unpaid_count)}</span>` : fmtNum.format(0)}</td>
        <td class="num">${u.time_join ? jalaliDateTime.format(new Date(u.time_join * 1000)) : "-"}</td>
        <td class="num">${u.last_message_time ? jalaliDateTime.format(new Date(u.last_message_time * 1000)) : "-"}</td>
        <td>${testChip(u.limit_usertest)}</td>
        <td><button class="pay-btn" data-chat-id="${u.chat_id}">مشاهده</button></td>
      </tr>`).join("");
    attachCopyHandlers(body);
    body.querySelectorAll(".pay-btn").forEach((btn) => {
      btn.addEventListener("click", () => openPaymentsModal(btn.dataset.chatId));
    });
  }

  function updateSortIndicators() {
    document.querySelectorAll('#users-table thead th[data-sort]').forEach((th) => {
      const ind = th.querySelector(".sort-ind");
      ind.textContent = th.dataset.sort === sortBy ? (sortDir === "asc" ? "▲" : "▼") : "";
    });
  }

  document.querySelectorAll('#users-table thead th[data-sort]').forEach((th) => {
    th.addEventListener("click", () => {
      const col = th.dataset.sort;
      if (sortBy === col) {
        sortDir = sortDir === "desc" ? "asc" : "desc";
      } else {
        sortBy = col;
        sortDir = "desc";
      }
      currentPage = 1;
      loadUsers();
    });
  });

  function renderPager() {
    const totalPages = Math.max(1, Math.ceil(lastTotal / lastLimit));
    document.getElementById("page-info").textContent = `صفحه ${currentPage} از ${fmtNum.format(totalPages)} (${fmtNum.format(lastTotal)} کاربر)`;
    document.getElementById("prev-page").disabled = currentPage <= 1;
    document.getElementById("next-page").disabled = currentPage >= totalPages;
  }

  async function loadUsers() {
    const body = document.getElementById("users-body");
    body.innerHTML = `<tr><td colspan="9" class="empty-hint">در حال بارگذاری...</td></tr>`;

    const params = new URLSearchParams({ page: String(currentPage), channel: getSelectedChannel() });
    if (currentFilter) params.set("filter", currentFilter);
    if (currentQuery) params.set("q", currentQuery);
    if (sortBy) { params.set("sort_by", sortBy); params.set("sort_dir", sortDir); }

    const resp = await fetch(`${BASE}/api/sales-users?${params.toString()}`);
    if (resp.status === 401) { location.href = `${BASE}/`; return; }
    const data = await resp.json();
    if (!data.status) return;

    lastTotal = data.obj.total;
    lastLimit = data.obj.limit;
    renderStats(data.obj.stats);
    renderUsersTable(data.obj.users);
    updateSortIndicators();
    renderPager();

    const when = data.obj.last_synced_at ? jalaliDateTime.format(new Date(data.obj.last_synced_at * 1000)) : "-";
    document.getElementById("sync-note").textContent =
      `${fmtNum.format(data.obj.synced_count)} کاربر همگام‌سازی‌شده · آخرین همگام‌سازی: ${when}`;
  }

  async function openPaymentsModal(chatId) {
    document.getElementById("payments-modal-title").textContent = `پرداخت‌های کاربر ${chatId}`;
    document.getElementById("payments-summary").innerHTML = "";
    document.getElementById("payments-body").innerHTML = `<tr><td colspan="5" class="empty-hint">در حال بارگذاری...</td></tr>`;
    document.getElementById("payments-modal-overlay").classList.add("open");

    const resp = await fetch(`${BASE}/api/sales-users/${chatId}/payments?channel=${encodeURIComponent(getSelectedChannel())}`);
    if (resp.status === 401) { location.href = `${BASE}/`; return; }
    const data = await resp.json();
    if (!data.status) return;

    const s = data.obj.summary;
    document.getElementById("payments-summary").innerHTML = `
      <span class="item">✅ تایید شده: ${fmtNum.format(s.paid)}</span>
      <span class="item">⏳ منقضی: ${fmtNum.format(s.expire)}</span>
      <span class="item">🕓 در انتظار: ${fmtNum.format(s.waiting)}</span>
      <span class="item">❌ رد شده: ${fmtNum.format(s.reject)}</span>
      <span class="item">سایر: ${fmtNum.format(s.other)}</span>
    `;

    const payments = data.obj.payments;
    const payBody = document.getElementById("payments-body");
    if (!payments.length) {
      payBody.innerHTML = `<tr><td colspan="5" class="empty-hint">پرداختی ثبت نشده</td></tr>`;
      return;
    }
    payBody.innerHTML = payments.map((p) => `
      <tr>
        <td>${copyable(p.id)}</td>
        <td class="num">${fmtNum.format(p.price)} تومان</td>
        <td>${p.payment_method || "-"}</td>
        <td class="num">${jalaliDateTime.format(new Date(p.payment_time * 1000))}</td>
        <td>${payStatusChip(p.payment_status)}</td>
      </tr>`).join("");
    attachCopyHandlers(payBody);
  }

  document.getElementById("filter-picker").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-f]");
    if (!btn) return;
    document.querySelectorAll("#filter-picker button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    currentFilter = btn.dataset.f;
    currentPage = 1;
    loadUsers();
  });

  document.getElementById("search-box").addEventListener("input", (e) => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => {
      currentQuery = e.target.value.trim();
      currentPage = 1;
      loadUsers();
    }, 400);
  });

  document.getElementById("prev-page").addEventListener("click", () => {
    if (currentPage > 1) { currentPage -= 1; loadUsers(); }
  });
  document.getElementById("next-page").addEventListener("click", () => {
    const totalPages = Math.max(1, Math.ceil(lastTotal / lastLimit));
    if (currentPage < totalPages) { currentPage += 1; loadUsers(); }
  });

  document.getElementById("payments-close-btn").addEventListener("click", () => {
    document.getElementById("payments-modal-overlay").classList.remove("open");
  });

  // ---- bulk block ----

  function collectBlockCriteria() {
    const purchaseFilter = document.querySelector('input[name="purchase-filter"]:checked').value;
    const depositFilter = document.querySelector('input[name="deposit-filter"]:checked').value;
    const minUnpaidRaw = document.getElementById("block-min-unpaid").value.trim();
    const minJoinAgeRaw = document.getElementById("block-min-join-age").value.trim();
    return {
      purchase_filter: purchaseFilter,
      deposit_filter: depositFilter,
      min_unpaid: minUnpaidRaw ? parseInt(minUnpaidRaw, 10) : null,
      min_join_age_days: minJoinAgeRaw ? parseInt(minJoinAgeRaw, 10) : null,
    };
  }

  function setBlockFormDisabled(disabled) {
    document.querySelectorAll('#block-modal-overlay input, #block-modal-overlay textarea, #block-modal-overlay button')
      .forEach((el) => { if (el.id !== "block-close-btn") el.disabled = disabled; });
  }

  function renderBlockPreview(obj) {
    document.getElementById("block-preview-note").textContent =
      `${fmtNum.format(obj.total)} کاربر با این فیلترها پیدا شد` + (obj.total > 30 ? " (فقط ۳۰ نمونه اول نشون داده می‌شه)" : "");
    const wrap = document.getElementById("block-preview-wrap");
    const body = document.getElementById("block-preview-body");
    if (!obj.sample.length) {
      wrap.style.display = "none";
    } else {
      wrap.style.display = "block";
      body.innerHTML = obj.sample.map((u) => `
        <tr>
          <td>${copyable(u.chat_id)}</td>
          <td>${u.username ? "@" + u.username : "-"}</td>
          <td class="num">${fmtNum.format(u.purchase_count)}</td>
          <td class="num">${fmtNum.format(u.deposit_count)}</td>
          <td class="num">${fmtNum.format(u.unpaid_count)}</td>
        </tr>`).join("");
      attachCopyHandlers(body);
    }
    document.getElementById("block-run-btn").disabled = obj.total === 0;
    document.getElementById("block-run-btn").dataset.total = obj.total;
  }

  async function previewBlock() {
    const note = document.getElementById("block-preview-note");
    note.textContent = "در حال محاسبه...";
    document.getElementById("block-run-btn").disabled = true;
    const resp = await fetch(`${BASE}/api/sales-users/block-preview?channel=${encodeURIComponent(getSelectedChannel())}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectBlockCriteria()),
    });
    if (resp.status === 401) { location.href = `${BASE}/`; return; }
    const data = await resp.json();
    if (!data.status) { note.textContent = data.msg || "خطا در محاسبه پیش‌نمایش"; return; }
    renderBlockPreview(data.obj);
  }

  async function runBlock() {
    const total = parseInt(document.getElementById("block-run-btn").dataset.total || "0", 10);
    const reason = document.getElementById("block-reason").value.trim();
    if (!reason) { alert("دلیل مسدودسازی رو بنویس."); return; }
    if (!total) { alert("اول پیش‌نمایش بگیر تا مطمئن بشیم کسی پیدا می‌شه."); return; }
    const confirmed = confirm(`مطمئنی؟ ${total} کاربر تو ربات فروش مسدود می‌شن با این دلیل:\n\n"${reason}"\n\nاین کار قابل بازگشت خودکار نیست.`);
    if (!confirmed) return;

    const resp = await fetch(`${BASE}/api/sales-users/block?channel=${encodeURIComponent(getSelectedChannel())}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...collectBlockCriteria(), reason }),
    });
    if (resp.status === 401) { location.href = `${BASE}/`; return; }
    const data = await resp.json();
    if (!data.status) { alert(data.msg || "شروع عملیات مسدودسازی ناموفق بود"); return; }
    setBlockFormDisabled(true);
    document.getElementById("block-close-btn").disabled = false;
  }

  function renderBlockProgress(evt) {
    const wrap = document.getElementById("block-progress-wrap");
    wrap.style.display = "block";
    const pct = evt.total > 0 ? (evt.done / evt.total) * 100 : 0;
    document.getElementById("block-progress-fill").style.width = pct.toFixed(1) + "%";
    let text = `${fmtNum.format(evt.done)} از ${fmtNum.format(evt.total)} · موفق: ${fmtNum.format(evt.ok)} · ناموفق: ${fmtNum.format(evt.failed)}`;
    if (!evt.running && evt.finished_at) {
      text += " · تمام شد";
      setBlockFormDisabled(false);
      loadUsers();
    }
    document.getElementById("block-progress-note").textContent = text;
  }

  async function syncBlockStatus() {
    const resp = await fetch(`${BASE}/api/sales-users/block-status?channel=${encodeURIComponent(getSelectedChannel())}`);
    if (resp.status === 401) return;
    const data = await resp.json();
    if (!data.status) return;
    if (data.obj.running) {
      setBlockFormDisabled(true);
      document.getElementById("block-close-btn").disabled = false;
      renderBlockProgress(data.obj);
    } else if (data.obj.total) {
      renderBlockProgress(data.obj);
    }
  }

  document.getElementById("open-block-btn").addEventListener("click", () => {
    document.getElementById("block-modal-overlay").classList.add("open");
    syncBlockStatus();
  });
  document.getElementById("block-close-btn").addEventListener("click", () => {
    document.getElementById("block-modal-overlay").classList.remove("open");
  });
  document.getElementById("block-preview-btn").addEventListener("click", previewBlock);
  document.getElementById("block-run-btn").addEventListener("click", runBlock);
  document.querySelectorAll(".chip-btn[data-days]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.getElementById("block-min-join-age").value = btn.dataset.days;
    });
  });

  function connectWS() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}${BASE}/ws`);
    ws.onmessage = (msg) => {
      let evt;
      try { evt = JSON.parse(msg.data); } catch (e) { return; }
      if (evt.type !== "sales_block") return;
      renderBlockProgress(evt);
    };
    ws.onclose = () => setTimeout(connectWS, 3000);
    ws.onerror = () => ws.close();
  }

  loadChannels();
  loadUsers();
  connectWS();
})();
