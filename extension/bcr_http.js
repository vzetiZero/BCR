/* bcr_http.js — gửi request tới app qua background service worker.
 *
 * Content script không gọi thẳng 127.0.0.1 được (Private Network Access + CORS chặn
 * request từ trang HTTPS công khai). Service worker của extension có host_permissions
 * nên gọi được. Nếu extension chưa có worker (bản cũ), tự rơi về fetch trực tiếp. */
(() => {
  "use strict";
  const BASE = "http://127.0.0.1:8777";

  async function bcrHttp(path, options = {}) {
    const url = BASE + path;
    const method = options.method || "GET";
    const headers = options.headers || {};
    const body = options.body;
    try {
      if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.id) {
        const reply = await chrome.runtime.sendMessage({
          kind: "bcr-http", url: url, method: method, headers: headers, body: body
        });
        if (reply && reply.ok !== undefined) return reply;
      }
    } catch (e) {
      /* extension context hỏng hoặc worker chưa nạp: thử fetch trực tiếp */
    }
    try {
      const res = await fetch(url, { method: method, headers: headers, body: body });
      const text = await res.text();
      let json = null;
      try { json = JSON.parse(text); } catch (e) { /* không phải JSON */ }
      return { ok: res.ok, status: res.status, json: json, text: text };
    } catch (e) {
      return { ok: false, error: String((e && e.name) || e) };
    }
  }

  window.__BCR_HTTP = bcrHttp;
})();
