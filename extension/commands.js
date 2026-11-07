/* BCR command bridge — nhận lệnh từ app (http://127.0.0.1:8777/command) và thao tác
 * THẬT trên DOM của chính Chrome/profile người dùng đang đăng nhập.
 *
 * Vì sao làm thế này: app không mở Chrome bằng chromedriver nữa (driver sinh
 * "Chrome is being controlled by automated test software" và dùng profile trắng, khiến
 * site đá phiên "Bạn đã đăng xuất"). Ở đây mọi thao tác là sự kiện chuột thật trên trang
 * thật, nên với site không khác gì người dùng bấm.
 *
 * Chỉ xử lý các lệnh trong danh sách trắng của app. Không có lệnh nào tự chuyển tiền. */
(() => {
  "use strict";

  const BASE = "http://127.0.0.1:8777";
  const POLL_MS = 1200;
  const IS_TOP = window.top === window.self;

  /* ---------- tiện ích DOM ---------- */
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function visibleRect(el) {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return null;
    const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
    if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) return null;
    return { x: Math.round(cx), y: Math.round(cy) };
  }

  /* Bấm bằng chuỗi sự kiện chuột thật để React/Canvas nhận như người dùng. */
  function realClick(el, point) {
    const p = point || visibleRect(el) || { x: 0, y: 0 };
    const base = { bubbles: true, cancelable: true, composed: true,
                   clientX: p.x, clientY: p.y, button: 0, buttons: 1 };
    try { el.scrollIntoView({ block: "center", inline: "center" }); } catch (e) { /* noop */ }
    const target = el || document.elementFromPoint(p.x, p.y) || document.body;
    for (const type of ["pointerover", "pointerenter", "pointerdown", "mousedown"]) {
      target.dispatchEvent(new PointerEvent(type, { ...base, pointerType: "mouse", isPrimary: true }));
    }
    for (const type of ["pointerup", "mouseup", "click"]) {
      target.dispatchEvent(new PointerEvent(type, { ...base, buttons: 0, pointerType: "mouse", isPrimary: true }));
    }
    return p;
  }

  function byText(selector, text) {
    const want = text.toLowerCase();
    return [...document.querySelectorAll(selector)]
      .find((el) => (el.textContent || "").toLowerCase().includes(want)) || null;
  }

  /* ---------- từng lệnh ---------- */
  const commands = {
    ping() {
      return { ok: true, url: location.href, top: IS_TOP };
    },

    open_lobby() {
      if (!IS_TOP) return { ok: false, reason: "not_top_frame" };
      // Trang gốc NEO79: bấm thẻ/thẻ game Evolution để mở sảnh thật.
      const card = byText('[data-role],button,a,div', "evolution") ||
                   byText('[data-role],button,a,div', "baccarat");
      if (!card) return { ok: false, reason: "evo_card_not_found" };
      const p = realClick(card);
      return { ok: true, clicked: p };
    },

    back_to_lobby() {
      const btn = document.querySelector('[data-role="lobby-button"]') ||
                  byText('button,[role="button"]', "sảnh") ||
                  byText('button,[role="button"]', "lobby");
      if (!btn) return { ok: false, reason: "lobby_button_not_found" };
      return { ok: true, clicked: realClick(btn) };
    },

    click_table(args) {
      const tableId = String(args.table_id || "");
      const label = String(args.label || "");
      let tile = null;
      if (tableId) {
        tile = [...document.querySelectorAll('[data-role="table-tile"]')]
          .find((el) => (el.getAttribute("data-table-id") || "").includes(tableId));
      }
      if (!tile) {
        tile = [...document.querySelectorAll('[data-role="table-tile"]')].find((el) =>
          label && (el.textContent || "").toLowerCase().includes(label.toLowerCase()));
      }
      if (!tile) return { ok: false, reason: "table_tile_not_found" };
      return { ok: true, clicked: realClick(tile) };
    },

    read_chips() {
      const tray = document.querySelector('[data-role="chip-stack"]');
      const chips = {};
      let selected = null;
      if (tray) {
        for (const el of tray.querySelectorAll('[data-role="chip"][data-value]')) {
          const v = parseInt(el.getAttribute("data-value"), 10);
          if (Number.isInteger(v) && v > 0 && !(v in chips)) {
            const r = visibleRect(el);
            if (r) chips[v] = r;
          }
        }
      }
      const sel = document.querySelector('[data-role="chip"][data-selected="true"]');
      if (sel) selected = parseInt(sel.getAttribute("data-value"), 10) || null;
      // Tổng chip đã đặt mỗi cửa — dùng để suy luận "ván mới" (cả 3 cửa trống nghĩa là
      // bàn vừa gom chip ván trước, chưa phải lúc đọc timer đặt cược thật).
      const spots = {};
      for (const [role, side] of [["Banker", "B"], ["Player", "P"], ["Tie", "T"]]) {
        const spot = document.querySelector('[data-role="bet-spot-' + role + '"]');
        let amount = 0;
        if (spot) {
          for (const c of spot.querySelectorAll('[data-role="chip"]')) {
            if (!visibleRect(c)) continue;
            const v = parseInt(c.getAttribute("data-value"), 10);
            if (Number.isInteger(v) && v > 0) amount += v;
          }
        }
        spots[side] = amount;
      }
      return { ok: true, chips: chips, selected: selected, spots: spots };
    },

    async place_chips(args) {
      const role = { B: "Banker", P: "Player", T: "Tie" }[String(args.side || "")];
      const chips = Array.isArray(args.chips) ? args.chips.map((c) => parseInt(c, 10)) : [];
      if (!role) return { ok: false, reason: "bad_side" };
      if (!chips.length || chips.some((c) => !Number.isInteger(c) || c <= 0)) {
        return { ok: false, reason: "bad_chips" };
      }
      const spot = document.querySelector('[data-role="bet-spot-' + role + '"]');
      if (!spot) return { ok: false, reason: "bet_spot_not_found" };
      const tray = document.querySelector('[data-role="chip-stack"]');
      if (!tray) return { ok: false, reason: "chip_tray_not_found" };

      const counts = {};
      for (const c of chips) counts[c] = (counts[c] || 0) + 1;
      const placed = {};
      for (const denom of Object.keys(counts).map(Number).sort((a, b) => b - a)) {
        const chipEl = [...tray.querySelectorAll('[data-role="chip"][data-value]')]
          .find((el) => parseInt(el.getAttribute("data-value"), 10) === denom);
        if (!chipEl) continue;
        realClick(chipEl);
        await sleep(260);
        for (let i = 0; i < counts[denom]; i++) {
          realClick(spot);
          await sleep(280);
        }
        placed[denom] = counts[denom];
      }
      const total = Object.entries(placed).reduce((s, [d, n]) => s + Number(d) * n, 0);
      return { ok: total > 0, placed: placed, total: total, reason: total ? "" : "denom_not_on_tray" };
    }
  };

  /* ---------- vòng nhận lệnh ---------- */
  async function poll() {
    const send = window.__BCR_HTTP || ((u, i) => fetch(u, i).then(async (r) => ({
      ok: r.ok, status: r.status, json: await r.json().catch(() => null)
    })));
    let command = null;
    try {
      const res = await send("/command", { method: "GET" });
      command = (res.json || {}).command;
    } catch (e) {
      return;   // app chưa chạy: thử lại nhịp sau
    }
    if (!command) return;
    let result;
    try {
      const fn = commands[command.name];
      result = fn ? await fn(command.args || {}) : { ok: false, reason: "unknown_command" };
    } catch (e) {
      result = { ok: false, reason: "exec_error:" + (e && e.name ? e.name : "Error") };
    }
    try {
      await send("/result", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: command.id, result })
      });
    } catch (e) { /* app vừa tắt */ }
  }

  if (IS_TOP) {
    setInterval(() => { if (!document.hidden) poll(); }, POLL_MS);
    setTimeout(poll, 1200);
  }
})();
