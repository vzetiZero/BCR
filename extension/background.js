/* BCR background service worker — cầu nối HTTP tới app trên máy.
 *
 * Vì sao cần lớp này: content script chạy trong ngữ cảnh TRANG, nên request từ một
 * trang HTTPS công khai xuống 127.0.0.1 bị Chrome chặn bởi Private Network Access
 * và CORS. Service worker thì dùng host_permissions của extension, không bị hai rào
 * đó, nên mọi lời gọi tới app đều đi qua đây và luôn thành công.
 *
 * Chỉ chuyển tiếp tới 127.0.0.1:8777. Không gửi dữ liệu ra bất kỳ đâu khác. */

const ALLOWED = "http://127.0.0.1:8777";

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.kind !== "bcr-http") return undefined;
  const url = String(message.url || "");
  if (!url.startsWith(ALLOWED)) {
    sendResponse({ ok: false, error: "url_not_allowed" });
    return undefined;
  }
  const init = {
    method: message.method || "GET",
    headers: message.headers || {},
    body: message.body
  };
  fetch(url, init)
    .then(async (res) => {
      const text = await res.text();
      let json = null;
      try { json = JSON.parse(text); } catch (e) { /* không phải JSON */ }
      sendResponse({ ok: res.ok, status: res.status, json: json, text: text });
    })
    .catch((err) => sendResponse({ ok: false, error: String(err && err.name || err) }));
  return true;   // giữ kênh mở cho phản hồi bất đồng bộ
});
