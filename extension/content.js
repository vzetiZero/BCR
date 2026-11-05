/* BCR 3-3 Road Reader — phần gác cổng URL + gửi cầu về app. Logic đọc cầu ở reader.js
 * (dùng chung với browser.scan()). Chỉ ĐỌC bảng cầu đang hiển thị trên trang và gửi về app trên
 * chính máy này (http://127.0.0.1:8777). Không bấm hộ, không đặt cược, không gửi đi đâu khác.
 * Script thoát ngay trên mọi trang không phải sòng bài, nên không can thiệp site khác.
 * Chrome 137+ chặn --load-extension; khi đó app đọc cầu thẳng qua driver, không cần extension này. */
(() => {
  "use strict";

  const ENDPOINT = "http://127.0.0.1:8777/report";
  const AUTO_MS = 8000;
  const TOP = (() => {
    try { return window.top.location.href; } catch (e) { return location.href; }
  })();

  function isTarget() {
    return /\/frontend\/evo\/|table_id=|see=baccarat|baccarat|luckybaccarat/i.test(TOP) ||
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
    if (!payload.tables.length) { setStatus("Không thấy bảng cầu nào trên frame này (" + kind + ")"); return; }
    payload.page.table_id = payload.page.table_id || tableId;
    payload.page.url = TOP.slice(0, 200);
    payload.diag.trigger = trigger;
    try {
      const res = await fetch(ENDPOINT, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      setStatus("Đã gửi " + (data.updated || 0) + " bàn (" + kind + ") · app biết " + (data.known || 0) + " bàn");
    } catch (e) {
      setStatus("Không gọi được app trên 127.0.0.1:8777 — chạy Chay_BCR.bat trước");
    }
  }

  /* ---------- overlay điều khiển (chỉ ở frame ngoài cùng) ---------- */
  let statusEl = null;
  function setStatus(msg) {
    const line = new Date().toLocaleTimeString("vi-VN") + " · " + msg;
    if (statusEl) statusEl.textContent = line;
    try { window.top.postMessage({ bcr: line }, "*"); } catch (e) { /* frame đơn lẻ */ }
  }
  function buildOverlay() {
    const box = document.createElement("div");
    box.style.cssText = "position:fixed;left:10px;bottom:10px;z-index:2147483647;background:#151b26;" +
      "color:#e8edf5;font:12px/1.5 system-ui,sans-serif;border:1px solid #2f3b4f;border-radius:8px;" +
      "padding:8px 10px;box-shadow:0 6px 20px rgba(0,0,0,.45);max-width:420px";
    const btn = document.createElement("button");
    btn.textContent = "Đọc cầu ngay";
    btn.style.cssText = "background:#c9a227;color:#12181f;border:0;border-radius:5px;padding:4px 10px;" +
      "font:600 12px system-ui,sans-serif;cursor:pointer;margin-right:6px";
    const toggle = document.createElement("button");
    let on = true;
    toggle.textContent = "Tự đọc: BẬT";
    toggle.style.cssText = "background:#243044;color:#dbe4f0;border:1px solid #384a66;border-radius:5px;" +
      "padding:4px 10px;font:12px system-ui,sans-serif;cursor:pointer";
    statusEl = document.createElement("div");
    statusEl.textContent = "Sẵn sàng. App phải đang chạy để nhận cầu.";
    statusEl.style.cssText = "margin-top:6px;color:#9fb0c6";
    box.appendChild(btn); box.appendChild(toggle); box.appendChild(statusEl);
    document.documentElement.appendChild(box);

    let timer = setInterval(() => { if (on) readAll("auto"); }, AUTO_MS);
    btn.addEventListener("click", () => readAll("manual"));
    toggle.addEventListener("click", () => {
      on = !on;
      toggle.textContent = "Tự đọc: " + (on ? "BẬT" : "TẮT");
      if (!on) { clearInterval(timer); timer = null; } else timer = setInterval(() => readAll("auto"), AUTO_MS);
    });
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
    if (d.bcr && statusEl) statusEl.textContent = d.bcr;
    if (d.bcrCmd === "read" && !statusEl) readAndSend("frame");
  });

  if (window.top === window.self) buildOverlay();
  setTimeout(() => readAndSend("boot"), 2500);
})();
