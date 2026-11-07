
(() => {
  "use strict";

  const ENDPOINT = "http://127.0.0.1:8777/report";
  const AUTO_MS = 8000;
  const TOP = (() => {
    try { return window.top.location.href; } catch (e) { return location.href; }
  })();

  function isTarget() {
    // Chỉ đọc ở SẢNH EVO hoặc TRANG BÀN có table_id. Các trang khác của cổng (nạp tiền,
    // khuyến mãi, tài khoản…) bị bỏ qua hoàn toàn: extension không đọc, không gửi gì.
    return /\/frontend\/evo\/|table_id=/i.test(TOP) ||
      /\/frontend\/evo\/|table_id=/i.test(location.href);
  }
  if (!isTarget()) return;
  if (typeof window.__BCR_SCAN !== "function") return;

  const tableId = (() => {
    const m = /table_id=([^&]+)/.exec(TOP) || /table_id=([^&]+)/.exec(location.href);
    return m ? decodeURIComponent(m[1]) : "";
  })();

  /* ---------- gửi về app ---------- */
  async function readAndSend(trigger) {
    if (document.hidden && trigger === "auto") return;
    const payload = window.__BCR_SCAN();
    const kind = payload.diag.kind;
    if (!payload.tables.length) {
      // Sảnh chỉ là danh sách bàn, không có bảng cầu: không báo lỗi ở đây.
      if (tableId)
        setStatus("Không thấy bảng cầu nào trên frame này (" + kind + ")");
      else
        setStatus("Đang ở sảnh, chưa mở bàn — mở một bàn để đọc cầu.");
      return;
    }
    payload.page.table_id = payload.page.table_id || tableId;
    payload.page.url = TOP.slice(0, 200);
    payload.diag.trigger = trigger;
    try {
      const send = window.__BCR_HTTP || ((url, init) => fetch(url, init));
      const res = await send("/report", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = res.json || {};
      if (res.ok) {
        setStatus("Đã gửi " + (data.updated || 0) + " bàn (" + kind + ") · app biết " + (data.known || 0) + " bàn");
      } else {
        setStatus("App chưa nhận được cầu (" + (data.error || res.status || res.error || "?") + ")");
      }
    } catch (e) {
      setStatus("Không gọi được app trên 127.0.0.1:8777 — chạy Chay_BCR.bat trước");
    }
  }

  /* ---------- trạng thái (không vẽ gì lên trang) ---------- */
  const IS_TOP = window.top === window.self;
  function setStatus(msg) {
    // Hộp điều khiển cũ che mất giao diện sàn, nên chỉ ghi log của DevTools.
    console.debug("[BCR]", msg);
  }
  function pingFrames() {
    document.querySelectorAll("iframe").forEach((f) => {
      try { f.contentWindow.postMessage({ bcrCmd: "read" }, "*"); } catch (e) { /* cross-origin */ }
    });
  }
  function readAll(trigger) {
    readAndSend(trigger);
    pingFrames();
  }

  window.addEventListener("message", (ev) => {
    const d = ev.data || {};
    if (d.bcrCmd === "read" && !IS_TOP) readAndSend("frame");
  });

  if (IS_TOP) setInterval(() => readAll("auto"), AUTO_MS);
  setTimeout(() => readAndSend("boot"), 2500);
})();
