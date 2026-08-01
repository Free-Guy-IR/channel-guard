(() => {
  "use strict";

  const BASE = window.ADMIN_BASE || "";
  document.getElementById("dash-link").href = `${BASE}/`;
  document.getElementById("surveys-link").href = `${BASE}/surveys`;
  document.getElementById("sales-link").href = `${BASE}/sales`;
  document.getElementById("sales-users-link").href = `${BASE}/sales-users`;
  document.getElementById("backup-link").href = `${BASE}/backup`;

  const TZ = "Asia/Tehran";
  const fmtDateTime = new Intl.DateTimeFormat("fa-IR-u-ca-persian", {
    timeZone: TZ, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const fmtClock = new Intl.DateTimeFormat("fa-IR-u-ca-persian", {
    timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  const fmtDate = (ts) => (ts ? fmtDateTime.format(new Date(ts * 1000)) : "-");

  const STATUS_LABELS = {
    online: "آنلاین", offline: "آفلاین (تلاش برای تعمیر خودکار)",
    installing: "در حال نصب...", error: "خطا",
  };

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

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }

  // ---------- nodes table ----------
  function renderNodes(nodes) {
    const body = document.getElementById("nodes-body");
    if (!nodes.length) {
      body.innerHTML = `<tr><td colspan="5" class="empty-hint">هنوز نودی اضافه نشده</td></tr>`;
      return;
    }
    body.innerHTML = nodes.map((n) => `
      <tr>
        <td>${escapeHtml(n.label)}</td>
        <td class="num" style="direction:ltr; text-align:right;">${escapeHtml(n.host)}:${n.ssh_port}</td>
        <td>
          <span class="status-badge ${n.status}"><span class="dot"></span>${STATUS_LABELS[n.status] || n.status}</span>
          ${n.status === "error" && n.last_error ? `<div class="err-note">${escapeHtml(n.last_error)}</div>` : ""}
        </td>
        <td class="num">${fmtDate(n.last_checked_at)}</td>
        <td style="white-space:nowrap;">
          ${n.status === "error" ? `<button class="action-btn" data-recheck="${n.id}">بررسی مجدد</button>` : ""}
          <button class="action-btn" data-manual="${n.id}">دستورات دستی</button>
          <button class="del-btn" data-id="${n.id}">حذف</button>
        </td>
      </tr>`).join("");

    body.querySelectorAll(".del-btn").forEach((btn) => {
      btn.addEventListener("click", () => deleteNode(btn.dataset.id));
    });
    body.querySelectorAll("[data-manual]").forEach((btn) => {
      btn.addEventListener("click", () => openManualModal(btn.dataset.manual));
    });
    body.querySelectorAll("[data-recheck]").forEach((btn) => {
      btn.addEventListener("click", () => recheckNode(btn.dataset.recheck, btn));
    });
  }

  async function loadNodes() {
    const resp = await fetch(`${BASE}/api/nodes`);
    if (resp.status === 401) {
      location.href = `${BASE}/`;
      return;
    }
    const data = await resp.json();
    if (!data.status) return;
    renderNodes(data.obj.nodes);
  }

  async function deleteNode(id) {
    if (!confirm("این نود حذف بشه؟ دسترسی SSH پنل به این سرور لغو می‌شود.")) return;
    await fetch(`${BASE}/api/nodes/${id}`, { method: "DELETE" });
    loadNodes();
  }

  async function recheckNode(id, btn) {
    btn.disabled = true;
    btn.textContent = "در حال بررسی...";
    try {
      await fetch(`${BASE}/api/nodes/${id}/recheck`, { method: "POST" });
    } finally {
      loadNodes();
    }
  }

  // ---------- manual-script modal ----------
  const modalOverlay = document.getElementById("modal-overlay");
  document.getElementById("modal-close-btn").addEventListener("click", () => modalOverlay.classList.remove("open"));
  document.getElementById("modal-copy-btn").addEventListener("click", async () => {
    const btn = document.getElementById("modal-copy-btn");
    const ok = await copyText(document.getElementById("modal-script").textContent);
    btn.textContent = ok ? "کپی شد!" : "کپی نشد - دستی انتخاب کن";
    setTimeout(() => { btn.textContent = "کپی اسکریپت"; }, 1500);
  });

  async function openManualModal(nodeId) {
    const resp = await fetch(`${BASE}/api/nodes/${nodeId}/manual-script`);
    const data = await resp.json();
    if (!data.status) return;
    document.getElementById("modal-guide").textContent = data.obj.guide;
    document.getElementById("modal-ssh-cmd").textContent = data.obj.ssh_test_cmd;
    document.getElementById("modal-script").textContent = data.obj.script;
    modalOverlay.classList.add("open");
  }

  // ---------- add-node form + live progress ----------
  const form = document.getElementById("add-node-form");
  const addBtn = document.getElementById("add-btn");
  const formMsg = document.getElementById("form-msg");
  const liveLogCard = document.getElementById("live-log-card");
  const liveLog = document.getElementById("live-log");
  const liveLogLabel = document.getElementById("live-log-label");

  let watchedNodeId = null;

  function appendLogLine(step, message) {
    const line = document.createElement("div");
    line.className = `step ${step}`;
    line.textContent = `[${fmtClock.format(new Date())}] ${message}`;
    liveLog.appendChild(line);
    liveLog.scrollTop = liveLog.scrollHeight;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    formMsg.textContent = "";
    formMsg.className = "form-msg";
    addBtn.disabled = true;
    addBtn.textContent = "در حال ارسال...";

    const label = document.getElementById("n-label").value.trim();

    try {
      const resp = await fetch(`${BASE}/api/nodes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          label,
          host: document.getElementById("n-host").value.trim(),
          ssh_port: parseInt(document.getElementById("n-ssh-port").value, 10) || 22,
          ssh_user: document.getElementById("n-ssh-user").value.trim() || "root",
          ssh_password: document.getElementById("n-ssh-password").value,
        }),
      });
      const data = await resp.json();
      if (!resp.ok || !data.status) {
        formMsg.textContent = data.msg || "خطا در افزودن نود";
        formMsg.classList.add("err");
        return;
      }

      watchedNodeId = data.obj.id;
      liveLog.innerHTML = "";
      liveLogLabel.textContent = `نود «${label}»`;
      liveLogCard.style.display = "block";
      appendLogLine("connecting", "درخواست ثبت شد، در انتظار شروع نصب...");

      formMsg.textContent = "نود اضافه شد — روند نصب رو بالا (نصب زنده) دنبال کن.";
      formMsg.classList.add("ok");
      document.getElementById("n-ssh-password").value = "";
      loadNodes();
    } catch (err) {
      formMsg.textContent = "خطا در ارتباط با سرور";
      formMsg.classList.add("err");
    } finally {
      addBtn.disabled = false;
      addBtn.textContent = "اضافه‌کردن نود";
    }
  });

  function connectWS() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}${BASE}/ws`);
    ws.onmessage = (msg) => {
      let evt;
      try { evt = JSON.parse(msg.data); } catch (e) { return; }
      if (evt.type !== "node_progress" || evt.node_id !== watchedNodeId) return;
      appendLogLine(evt.step, evt.message);
      if (evt.step === "done" || evt.step === "failed" || evt.step === "connect_failed") {
        loadNodes();
      }
    };
    ws.onclose = () => setTimeout(connectWS, 3000);
    ws.onerror = () => ws.close();
  }

  loadNodes();
  setInterval(loadNodes, 5000);
  connectWS();
})();
