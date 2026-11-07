
(() => {
  "use strict";

  const ENDPOINT = "http://127.0.0.1:8777/balance";
  const PROFILE_PATH = "/vb9prod-ecp/api/v1/profile";
  const AUTO_MS = 15000;

  /* Không gắn với một trang chủ cụ thể: chỉ chạy khi trang hiện tại thực sự có API
   * profile của cổng (tự dò từ chính request/chuyển hướng của trang). Nếu không dò
   * được, extension im lặng — không gửi gì đi đâu. */
  const API_HOST_HINT = /api\d*\./i;

  function apiOrigin() {
    // Ưu tiên máy chủ API cùng gốc nếu trang đang ở chính máy chủ đó.
    if (location.hostname.split(".").length > 2 && /^api/i.test(location.hostname)) {
      return location.origin;
    }
    // Thử đổi tiền tố "www"/tên thường sang "api" trên cùng tên miền gốc.
    const parts = location.hostname.split(".");
    if (parts.length >= 2) {
      const root = parts.slice(-2).join(".");
      return location.protocol + "//api." + root;
    }
    return location.origin;
  }

  function looksLikePortal() {
    return /^https?:$/.test(location.protocol) && !!location.hostname;
  }

  if (!looksLikePortal()) return;

  /* Token phiên: ưu tiên localStorage/ sessionStorage các khoá hay dùng, rồi tới cookie.
   * Không hard-code token: nó hết hạn sau vài giờ và là bí mật của người dùng. */
  function findToken() {
    const keys = ["token", "access_token", "accessToken", "Authorization", "auth_token", "jwt"];
    for (const store of [window.localStorage, window.sessionStorage]) {
      try {
        for (const key of keys) {
          const raw = store.getItem(key);
          if (!raw) continue;
          const value = raw.replace(/^"|"$/g, "");
          if (value.length > 20) return value.replace(/^Bearer\s+/i, "");
        }
        for (let i = 0; i < store.length; i++) {
          const key = store.key(i) || "";
          if (!/token|auth/i.test(key)) continue;
          const raw = (store.getItem(key) || "").replace(/^"|"$/g, "");
          if (raw.length > 20 && raw.split(".").length === 3) return raw;
        }
      } catch (e) { /* storage bị chặn */ }
    }
    const match = /(?:^|;\s*)(?:token|access_token|Authorization)=([^;]{20,})/.exec(document.cookie || "");
    return match ? decodeURIComponent(match[1]).replace(/^Bearer\s+/i, "") : "";
  }

  /* Đọc số dư ngay trên giao diện sàn: <span data-role="header-balance">3.014,74 VN2</span>.
   * Đây là nguồn ưu tiên vì nó đúng bằng con số người dùng nhìn thấy, không cần token,
   * không phụ thuộc API nào. Trả về {value, currency} hoặc null. */
  function readDomBalance() {
    const el = document.querySelector('[data-role="header-balance"]');
    if (!el) return null;
    const raw = (el.textContent || "").replace(/[\u2066\u2069\u00a0]/g, " ").trim();
    // Dạng Việt Nam: 3.014,74 nghĩa là 3014.74 (dấu . hàng nghìn, dấu , thập phân).
    const match = raw.match(/-?\d[\d.,]*/);
    if (!match) return null;
    let text = match[0];
    if (text.includes(",") && text.includes(".")) {
      text = text.replace(/\./g, "").replace(",", ".");
    } else if (text.includes(",")) {
      const parts = text.split(",");
      text = parts.length === 2 && parts[1].length <= 2
        ? parts[0].replace(/\./g, "") + "." + parts[1]
        : text.replace(/,/g, "");
    } else {
      text = text.replace(/\./g, "");
    }
    const value = Number(text);
    if (!Number.isFinite(value)) return null;
    const currency = (raw.match(/[A-Z]{2,4}\d?/) || [""])[0];
    return { value: value, currency: currency };
  }

  async function sendBalance(value, currency, trigger, playerId, name, vip) {
    const payload = {
      balance: value,
      currency: currency || "",
      player: playerId || "",
      name: name || "",
      vip: vip || "",
      source: trigger
    };
    const sent = await (window.__BCR_HTTP || ((u, i) => fetch(u, i)))("/balance", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const out = sent.json || {};
    if (sent.ok) {
      setStatus("Số dư thật: " + value.toFixed(2) + " " + (currency || "") + " · đã gửi app");
    } else {
      setStatus("App từ chối số dư: " + (out.error || sent.status));
    }
  }

  async function readBalance(trigger) {
    // 1) Ưu tiên đúng con số hiển thị trên giao diện sàn.
    const dom = readDomBalance();
    if (dom) {
      await sendBalance(dom.value, dom.currency, trigger);
      return;
    }
    const token = findToken();
    if (!token) {
      setStatus("Chưa thấy ô số dư trên trang và cũng chưa có token phiên.");
      return;
    }
    try {
      const res = await fetch(apiOrigin() + PROFILE_PATH, {
        method: "GET",
        credentials: "omit",
        headers: { "Accept": "application/json", "Authorization": token }
      });
      if (!res.ok) {
        setStatus("API profile trả " + res.status + " — phiên có thể đã hết hạn.");
        return;
      }
      const data = await res.json();
      const wallets = Array.isArray(data.wallets) ? data.wallets : [];
      const main = wallets.find((w) => w && w.wallet === "MAIN") || wallets[0];
      const balance = main ? Number(main.balance) : NaN;
      if (!Number.isFinite(balance)) {
        setStatus("Không đọc được số dư ví MAIN trong phản hồi profile.");
        return;
      }
      const playerId = String(data?.player?.playerid || "");
      const name = String(data?.player?.firstname || "").trim();
      // VIP: trường mới là JSON nhiều ngôn ngữ, nên lấy tên mức đã hiển thị nếu có.
      const vip = String(data?.player?.newVipLevelName || "").match(/"vi-VN"\s*:\s*"([^"]+)"/)?.[1]
        || (data?.player?.newVipLevel != null ? "VIP" + data.player.newVipLevel : "");
      await sendBalance(balance, String(data?.player?.currency || ""), trigger,
                        playerId.length >= 4 ? "****" + playerId.slice(-4) : "", name, vip);
    } catch (e) {
      setStatus("Không gọi được app trên 127.0.0.1:8777 — chạy Chay_BCR.bat trước");
    }
  }

  let statusEl = null;
  function setStatus(msg) {
    const line = new Date().toLocaleTimeString("vi-VN") + " · " + msg;
    if (statusEl) statusEl.textContent = line;
    try { window.top.postMessage({ bcrBalance: line }, "*"); } catch (e) { /* frame */ }
  }

  if (window.top === window.self) {
    setTimeout(() => readBalance("boot"), 3000);
    setInterval(() => {
      if (!document.hidden) readBalance("auto");
    }, AUTO_MS);
    // Đọc ngay khi tab được hiện lại hoặc có thao tác, để số dư không bị cũ.
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) readBalance("visible");
    });
  }
})();
