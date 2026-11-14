// Renderer: mỗi trò một khối state riêng, chỉ vẽ lại phần của trò đó.
const api = window.bcr;
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = {
  game: 'longlong',
  longlong: null,
  baccarat: null,
  logs: { longlong: [], baccarat: [] },
  sub: { longlong: 'play', baccarat: 'play' },
};

/* ------------------------------- định dạng ------------------------------- */
const fmtVn2 = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const text = Number(value).toLocaleString('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${text} VN2`;
};
const fmtUnits = (value, digits = 0) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return Number(value).toLocaleString('de-DE', { maximumFractionDigits: digits });
};
const fmtVnd = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return `${Number(value).toLocaleString('de-DE', { maximumFractionDigits: 0 })}đ`;
};
const fmtTime = (seconds) => {
  if (!seconds) return '—';
  const date = new Date(Number(seconds) * 1000);
  return date.toLocaleTimeString('vi-VN', { hour12: false });
};

function toast(message) {
  const box = $('#toast');
  box.textContent = message;
  box.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.remove('show'), 2600);
}

/* --------------------------------- gọi API -------------------------------- */
async function call(game, method, params = {}) {
  const reply = await api.call(game, method, params);
  const payloadError = reply && reply.result && reply.result.ok === false
    ? reply.result.error || 'engine từ chối lệnh'
    : null;
  if (!reply || reply.ok === false || payloadError) {
    const text = (reply && reply.error) || payloadError || 'lỗi không rõ';
    appendLog(game, 'error', `${method}: ${text}`);
    toast(text);
    return null;
  }
  return reply.result;
}

/* ---------------------------------- log ---------------------------------- */
function appendLog(game, level, text) {
  const list = state.logs[game];
  list.push({ at: new Date().toLocaleTimeString('vi-VN', { hour12: false }), level, text });
  if (list.length > 300) list.shift();
  renderLog(game);
}

function renderLog(game) {
  const box = $(`#${game === 'longlong' ? 'll' : 'ba'}-log`);
  if (!box) return;
  const list = state.logs[game];
  if (!list.length) {
    box.innerHTML = '<div class="log-line info"><span class="m">Chưa có dòng nào.</span></div>';
    return;
  }
  box.innerHTML = list
    .map((entry) => `<div class="log-line ${entry.level}"><span class="t">${entry.at}</span><span class="m">${escapeHtml(entry.text)}</span></div>`)
    .join('');
  box.scrollTop = box.scrollHeight;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

/* ------------------------------- điều hướng ------------------------------- */
function switchGame(game) {
  state.game = game;
  $$('.game').forEach((button) => button.classList.toggle('active', button.dataset.game === game));
  $$('.view').forEach((view) => view.classList.toggle('hidden', view.id !== `view-${game}`));
  switchSub(game, state.sub[game] || 'play');
}

function switchSub(game, sub) {
  state.sub[game] = sub;
  const nav = $(`[data-subtabs="${game}"]`);
  $$('button', nav).forEach((button) => button.classList.toggle('active', button.dataset.sub === sub));
  $$(`.subview[data-game="${game}"]`).forEach((view) => view.classList.toggle('hidden', view.dataset.sub !== sub));
}

/* ------------------------------ render LONG-LONG ------------------------------ */
function renderLongLong(snapshot) {
  if (!snapshot) return;
  state.longlong = snapshot;

  const balance = snapshot.balance_vn2;
  const balanceBox = $('#ll-balance');
  balanceBox.textContent = fmtVn2(balance);
  balanceBox.className = 'kpi-value ' + (balance === null || balance === undefined ? '' : balance > 0 ? 'gold' : 'red');
  $('#ll-balance-note').textContent = snapshot.read_error
    ? `lỗi đọc: ${snapshot.read_error}`
    : `${fmtVnd(balance === null || balance === undefined ? null : balance * 1000)} · cập nhật ${fmtTime(snapshot.read_at)}`;

  const ladder = snapshot.ladder || [];
  $('#ll-step').textContent = `${Math.min(snapshot.step + 1, ladder.length || 1)} / ${ladder.length || 0}`;
  $('#ll-step-note').textContent = `mức kế tiếp ${fmtUnits(snapshot.step_stake)} đv · ${fmtVnd((snapshot.step_stake || 0) * 1000)}`;

  const profit = snapshot.profit_units || 0;
  const profitBox = $('#ll-profit');
  profitBox.textContent = `${profit >= 0 ? '+' : '-'}${fmtUnits(Math.abs(profit))} đv`;
  profitBox.className = 'kpi-value ' + (profit >= 0 ? 'green' : 'red');
  $('#ll-hands').textContent = `${snapshot.hands_seen || 0} ván đã thấy`;

  $('#ll-total').textContent = fmtVn2(snapshot.total_bet_vn2);
  $('#ll-phase').textContent = snapshot.status || 'chưa có ván';

  // Kế hoạch tay kế tiếp
  const plan = snapshot.plan || {};
  const planState = $('#ll-plan-state');
  const planBox = $('#ll-plan');
  if (snapshot.stopped || plan.action === 'stop') {
    planState.textContent = 'DỪNG PHIÊN';
    planState.className = 'hero-state stop';
    planBox.textContent = 'Đã chạm đáy thang — bấm “Reset phiên” để chạy lại.';
  } else if (plan.action === 'bet') {
    planState.textContent = 'ĐẶT';
    planState.className = 'hero-state bet';
    planBox.textContent = `Đặt ${fmtUnits(plan.stake)} đv vào ô “1 vàng 1 đỏ” (bậc ${(plan.step || 0) + 1})`;
  } else {
    planState.textContent = 'CHỜ';
    planState.className = 'hero-state';
    planBox.textContent = snapshot.read_error ? 'Chưa đọc được bàn.' : 'Chờ tay ra bộ vàng-đỏ (VD/DV).';
  }
  $('#ll-plan-reason').textContent = plan.reason || '';

  // Ba ô cược
  const doors = snapshot.doors || {};
  ['JNS_B', 'JNS_C', 'JNS_A'].forEach((role) => {
    const door = doors[role] || {};
    const card = $(`#door-${role}`);
    if (card) card.classList.toggle('win', snapshot.winner === role);
    const amount = $(`[data-door-amount="${role}"]`);
    const foot = $(`[data-door-foot="${role}"]`);
    if (amount) amount.textContent = fmtVn2(door.amount);
    if (foot) {
      const bits = [];
      if (door.chips !== undefined && door.chips !== null) bits.push(`${door.chips} chip bàn`);
      if (door.percent !== undefined && door.percent !== null) bits.push(`${door.percent}%`);
      if (door.my_chips) bits.push(`${door.my_chips} chip của bạn`);
      foot.textContent = bits.join(' · ') || '—';
    }
  });

  // Thang cược
  const strip = $('#ll-ladder-strip');
  if (strip) {
    strip.innerHTML = ladder
      .map((value, index) => {
        const cls = index === snapshot.step ? 'now' : index < snapshot.step ? 'done' : '';
        return `<div class="ls-cell ${cls}"><b>${fmtUnits(value)}</b><small>bậc ${index + 1}</small></div>`;
      })
      .join('');
  }

  // Chip trạng thái + nút
  const chromeChip = $('#ll-chrome-chip');
  chromeChip.textContent = snapshot.chrome_connected ? 'Chrome: đã kết nối' : 'Chrome: chưa kết nối';
  chromeChip.dataset.state = snapshot.chrome_connected ? 'on' : 'off';
  const toolChip = $('#ll-tool-chip');
  toolChip.textContent = snapshot.tool_running ? 'Tool: đang chạy' : 'Tool: đang dừng';
  toolChip.dataset.state = snapshot.tool_running ? 'on' : 'warn';
  const toolButton = $('#ll-tool');
  toolButton.textContent = snapshot.tool_running ? 'Dừng tool' : 'Chạy tool';
  toolButton.classList.toggle('danger-btn', !!snapshot.tool_running);
  $('#ll-open-chrome').disabled = !!snapshot.chrome_connected;
  $('[data-led="longlong"]').classList.toggle('on', !!snapshot.tool_running || !!snapshot.chrome_connected);

  // Cấu hình
  const config = snapshot.config || {};
  if (document.activeElement !== $('#ll-base')) $('#ll-base').value = config.base ?? '';
  if (document.activeElement !== $('#ll-capital')) $('#ll-capital').value = config.capital ?? '';
  if (document.activeElement !== $('#ll-min-gap')) $('#ll-min-gap').value = config.min_gap ?? 15;
  $('#ll-place-real').checked = !!config.place_real;
  $('#ll-auto-result').checked = config.auto_result !== false;

  const edit = $('#ll-ladder-edit');
  const signature = ladder.join(',');
  if (edit && edit.dataset.signature !== signature) {
    edit.dataset.signature = signature;
    edit.innerHTML = ladder
      .map((value, index) => `<label>Bậc ${index + 1}<input data-ladder="${index}" type="number" min="1" step="10" value="${value}" /></label>`)
      .join('');
  }

  // Bảng log trong snapshot (chỉ để đối chiếu, không thay log sự kiện)
  if (snapshot.log && snapshot.log.length) state.lastHandLog = snapshot.log;
}

/* ------------------------------ render BACCARAT ------------------------------ */
function renderBaccarat(snapshot) {
  if (!snapshot) return;
  state.baccarat = snapshot;
  const chromeChip = $('#ba-chrome-chip');
  chromeChip.textContent = snapshot.chrome_connected ? 'Chrome: đã kết nối' : 'Chrome: chưa kết nối';
  chromeChip.dataset.state = snapshot.chrome_connected ? 'on' : 'off';
  const toolChip = $('#ba-tool-chip');
  toolChip.textContent = snapshot.tool_running ? 'Tool: đang chạy' : 'Tool: đang dừng';
  toolChip.dataset.state = snapshot.tool_running ? 'on' : 'warn';
  const toolButton = $('#ba-tool');
  toolButton.textContent = snapshot.tool_running ? 'Dừng tool' : 'Chạy tool';
  toolButton.classList.toggle('danger-btn', !!snapshot.tool_running);
  $('#ba-open-chrome').disabled = !!snapshot.chrome_connected;
  $('[data-led="baccarat"]').classList.toggle('on', !!snapshot.tool_running || !!snapshot.chrome_connected);

  const list = $('#ba-tables');
  const rows = snapshot.tables || [];
  if (!rows.length) {
    list.innerHTML = `<p class="hint">${snapshot.read_error ? escapeHtml(snapshot.read_error) : 'Chưa quét. Mở Chrome, vào sảnh Baccarat rồi bấm “Quét ngay”.'}</p>`;
  } else {
    list.innerHTML = rows
      .map((row) => `<div class="table-row"><div><b>${escapeHtml(row.label || row.id || '?')}</b>
        <small>${escapeHtml(row.id || '')}</small></div>
        <small>${escapeHtml(row.letters || row.note || '')}</small></div>`)
      .join('');
  }
}

/* --------------------------------- sự kiện --------------------------------- */
api.onEngineEvent(({ game, event, data }) => {
  if (event === 'state') {
    if (game === 'longlong') renderLongLong(data);
    else renderBaccarat(data);
    return;
  }
  if (event === 'log') {
    appendLog(game, data.level || 'info', data.text || '');
    return;
  }
  if (event === 'ready') {
    appendLog(game, 'info', `engine ${data.game} sẵn sàng · CDP ${data.cdp_port}`);
  }
});

document.addEventListener('click', (event) => {
  const winButton = event.target.closest('[data-win]');
  if (winButton) api.windowControl(winButton.dataset.win);
});

$$('.game').forEach((button) => button.addEventListener('click', () => switchGame(button.dataset.game)));

$$('[data-subtabs]').forEach((nav) => {
  const game = nav.dataset.subtabs;
  $$('button', nav).forEach((button) => button.addEventListener('click', () => switchSub(game, button.dataset.sub)));
});

/* LONG-LONG actions */
$('#ll-open-chrome').addEventListener('click', async () => {
  await call('longlong', 'chrome.open');
  toast('Đang mở Chrome cho LONG-LONG — đăng nhập rồi vào bàn.');
});

$('#ll-tool').addEventListener('click', async () => {
  const running = state.longlong && state.longlong.tool_running;
  const reply = await call('longlong', running ? 'tool.stop' : 'tool.start');
  if (reply) toast(running ? 'Đã dừng tool.' : 'Tool đang chạy — theo dõi ở tab Bàn chơi.');
});

$('#ll-save').addEventListener('click', async () => {
  const ladder = $$('#ll-ladder-edit input').map((input) => Number(input.value)).filter((value) => value > 0);
  await call('longlong', 'config.set', {
    base: Number($('#ll-base').value),
    capital: Number($('#ll-capital').value),
    min_gap: Number($('#ll-min-gap').value),
    place_real: $('#ll-place-real').checked,
    auto_result: $('#ll-auto-result').checked,
    ladder,
  });
  toast('Đã lưu cấu hình LONG-LONG.');
});

$('#ll-divide').addEventListener('click', async () => {
  await call('longlong', 'ladder.fromCapital', {
    capital: Number($('#ll-capital').value),
    base: Number($('#ll-base').value),
  });
  toast('Đã chia thang theo vốn.');
});

$('#ll-place-real').addEventListener('change', async (event) => {
  const on = event.target.checked;
  await call('longlong', 'config.set', { place_real: on });
  toast(on ? 'ĐÃ BẬT đặt chip thật — tool sẽ bấm chip vào ô C.' : 'Đã tắt đặt chip thật (chỉ mô phỏng).');
});

$('#ll-auto-result').addEventListener('change', async (event) => {
  await call('longlong', 'config.set', { auto_result: event.target.checked });
});

$('#ll-reset').addEventListener('click', async () => {
  await call('longlong', 'session.reset');
  toast('Đã reset phiên LONG-LONG.');
});

$('#ll-clear-log').addEventListener('click', () => {
  state.logs.longlong = [];
  renderLog('longlong');
});

/* Baccarat actions */
$('#ba-open-chrome').addEventListener('click', async () => {
  await call('baccarat', 'chrome.open');
  toast('Đang mở Chrome cho Baccarat.');
});

$('#ba-tool').addEventListener('click', async () => {
  const running = state.baccarat && state.baccarat.tool_running;
  await call('baccarat', running ? 'tool.stop' : 'tool.start');
  toast(running ? 'Đã dừng tool Baccarat.' : 'Tool Baccarat đang chạy.');
});

$('#ba-scan').addEventListener('click', async () => {
  await call('baccarat', 'scan');
  toast('Đã quét sàn Baccarat.');
});

$('#ba-clear-log').addEventListener('click', () => {
  state.logs.baccarat = [];
  renderLog('baccarat');
});

/* ---------------------------------- khởi động ---------------------------------- */
(async function boot() {
  renderLog('longlong');
  renderLog('baccarat');
  const info = await api.info();
  $('#app-ver').textContent = `v${info.version}`;
  switchGame('longlong');
  const ll = await call('longlong', 'state');
  if (ll) renderLongLong(ll);
  const ba = await call('baccarat', 'state');
  if (ba) renderBaccarat(ba);
})();
