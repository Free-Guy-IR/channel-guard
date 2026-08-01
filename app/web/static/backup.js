(() => {
  "use strict";

  const BASE = window.ADMIN_BASE || "";
  document.getElementById("dash-link").href = `${BASE}/`;
  document.getElementById("nodes-link").href = `${BASE}/nodes`;
  document.getElementById("surveys-link").href = `${BASE}/surveys`;
  document.getElementById("sales-link").href = `${BASE}/sales`;
  document.getElementById("sales-users-link").href = `${BASE}/sales-users`;

  const TZ = "Asia/Tehran";
  const fmtDateTime = new Intl.DateTimeFormat("fa-IR-u-ca-persian", {
    timeZone: TZ, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  const fmtDate = (ts) => (ts ? fmtDateTime.format(new Date(ts * 1000)) : "—");
  const fmtSize = (bytes) => {
    if (!bytes && bytes !== 0) return "—";
    const mb = bytes / (1024 * 1024);
    return `${mb.toFixed(1)} مگابایت`;
  };

  function redirectToLoginIfUnauthorized(resp) {
    if (resp.status === 401) {
      window.location.href = `${BASE}/`;
      return true;
    }
    return false;
  }

  async function loadStatus() {
    const resp = await fetch(`${BASE}/api/backup/status`);
    if (redirectToLoginIfUnauthorized(resp)) return;
    const data = await resp.json();
    if (!data.status) return;
    const s = data.obj;
    document.getElementById("st-last-run").textContent = fmtDate(s.last_finished_at);
    let resultText = "—";
    if (s.running) resultText = "⏳ در حال اجرا...";
    else if (s.last_ok === true) resultText = "✅ موفق";
    else if (s.last_ok === false) resultText = `❌ خطا: ${s.last_error || ""}`;
    document.getElementById("st-result").textContent = resultText;
    document.getElementById("st-size").textContent = fmtSize(s.last_size_bytes);
    document.getElementById("st-duration").textContent = s.last_duration_s ? `${s.last_duration_s} ثانیه` : "—";

    document.getElementById("s-interval-value").value = s.interval_value || 6;
    document.getElementById("s-interval-unit").value = s.interval_unit || "hours";

    document.getElementById("run-btn").disabled = !!s.running;
    if (s.running) {
      setTimeout(loadStatus, 3000);
    }
  }

  async function loadSettings() {
    const resp = await fetch(`${BASE}/api/backup/settings`);
    if (redirectToLoginIfUnauthorized(resp)) return;
    const data = await resp.json();
    if (!data.status) return;
    document.getElementById("s-chat-id").value = data.obj.admin_chat_id;
    document.getElementById("s-interval-value").value = data.obj.interval_value;
    document.getElementById("s-interval-unit").value = data.obj.interval_unit;
  }

  document.getElementById("run-btn").addEventListener("click", async () => {
    const btn = document.getElementById("run-btn");
    const msg = document.getElementById("run-msg");
    btn.disabled = true;
    msg.textContent = "";
    msg.className = "form-msg";
    try {
      const resp = await fetch(`${BASE}/api/backup/run`, { method: "POST" });
      if (redirectToLoginIfUnauthorized(resp)) return;
      const data = await resp.json();
      if (!data.status) {
        msg.textContent = data.msg || "خطا در شروع بکاپ";
        msg.className = "form-msg err";
        btn.disabled = false;
        return;
      }
      msg.textContent = data.obj.already_running
        ? "یک بکاپ در حال اجراست..."
        : "بکاپ شروع شد - وقتی تمام شد از طریق ربات تلگرام برات ارسال می‌شه.";
      msg.className = "form-msg ok";
      setTimeout(loadStatus, 1500);
    } catch (e) {
      msg.textContent = "خطا در ارتباط با سرور";
      msg.className = "form-msg err";
      btn.disabled = false;
    }
  });

  document.getElementById("settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("save-settings-btn");
    const msg = document.getElementById("settings-msg");
    btn.disabled = true;
    msg.textContent = "";
    msg.className = "form-msg";

    const payload = {
      admin_chat_id: document.getElementById("s-chat-id").value.trim(),
      interval_value: document.getElementById("s-interval-value").value,
      interval_unit: document.getElementById("s-interval-unit").value,
    };
    const botToken = document.getElementById("s-bot-token").value.trim();
    if (botToken) payload.bot_token = botToken;

    try {
      const resp = await fetch(`${BASE}/api/backup/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (redirectToLoginIfUnauthorized(resp)) return;
      const data = await resp.json();
      if (!data.status) {
        msg.textContent = data.msg || "خطا در ذخیره تنظیمات";
        msg.className = "form-msg err";
      } else {
        msg.textContent = "ذخیره شد";
        msg.className = "form-msg ok";
        document.getElementById("s-bot-token").value = "";
        loadStatus();
      }
    } catch (e) {
      msg.textContent = "خطا در ارتباط با سرور";
      msg.className = "form-msg err";
    } finally {
      btn.disabled = false;
    }
  });

  loadSettings();
  loadStatus();
})();
