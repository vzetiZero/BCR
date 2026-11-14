// Renderer: mỗi trò một khối state riêng, chỉ vẽ lại phần của trò đó.
// LONG-LONG: bàn chơi / cấu hình / nhật ký.
// BACCARAT: bàn chơi / cấu hình / lịch sử / nhật ký (port từ giao diện Tk cũ).
const api = window.bcr;
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

// Gom lỗi runtime để kiểm tra nhanh khi chạy thử (không ảnh hưởng vận hành).
window.__errors = [];
window.addEventListener('error', (event) => window.__errors.push(String(event.message || event.error)));
window.addEventListener('unhandledrejection', (event) => window.__errors.push(String(event.reason)));

const state = {
  game: 'baccarat',
  longlong: null,
  baccarat: null,
  logs: { longlong: [], baccarat: [] },
  sub: { longlong: 'play', baccarat: 'play' },
  tableSearch: '',
  onlyEligible: false,
  lastLadderSignature: '',
  huntTick: 0,
};

/* ------------------------------- định dạng ------------------------------- */
const fmtVn2 = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const text = Number(value).toLocaleString('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${text} VN2`;
};
const fmtMoney = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return `${Number(value).toLocaleString('de-DE', { maximumFractionDigits: 2 })} VN2`;
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
  return new Date(Number(seconds) * 1000).toLocaleTimeString('vi-VN', { hour12: false });
};
const fmtSigned = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const number = Number(value);
  return `${number > 0 ? '+' : ''}${number.toLocaleString('de-DE', { maximumFractionDigits: 2 })}`;
};
const pct = (value) => `${Number(value || 0).toFixed(2)}%`;
const SIDE_NAME = { B: 'Banker', P: 'Player', T: 'Hoà' };

function toast(message) {
  const box = $('#toast');
  if (!box) return;
  box.textContent = message;
  box.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.remove('show'), 2800);
}

/* --------------------------------- gọi API -------------------------------- */
async function call(game, method, params = {}) {
  const reply = await api.call(game, method, params);
  const payloadError = reply && reply.result && reply.result.ok === false
    ? reply.result.error || reply.result.note || 'engine từ chối lệnh'
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
  if (list.length > 400) list.shift();
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

// Nút "Mở Chrome" phải luôn bấm lại được: người dùng có thể đóng cửa sổ Chrome
// bất cứ lúc nào, khi đó chỉ đổi nhãn thành "Mở lại Chrome".
const chromeSeen = { longlong: false, baccarat: false };

function syncOpenChromeButton(selector, game, snapshot) {
  const button = $(selector);
  if (!button) return;
  if (snapshot.chrome_connected) chromeSeen[game] = true;
  button.disabled = false;
  button.textContent = (!snapshot.chrome_connected && chromeSeen[game])
    ? 'Mở lại Chrome' : 'Mở Chrome';
  button.title = snapshot.chrome_connected
    ? 'Chrome đang kết nối — nếu bạn đã đóng cửa sổ, bấm để mở lại'
    : (chromeSeen[game] ? 'Chrome đã đóng — bấm để mở lại' : 'Chrome chưa kết nối — bấm để mở');
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
  if (nav) {
    $$('button', nav).forEach((button) => button.classList.toggle('active', button.dataset.sub === sub));
  }
  $$(`.subview[data-game="${game}"]`).forEach((view) => view.classList.toggle('hidden', view.dataset.sub !== sub));
  // Chart vẽ bằng canvas nên chỉ đo được kích thước khi tab hiện ra.
  if (game === 'longlong' && sub === 'stats' && state.longlong) renderLongLongStats(state.longlong);
}

/* ============================== LONG-LONG ============================== */
function renderLongLong(snapshot) {
  if (!snapshot) return;
  state.longlong = snapshot;

  const balance = snapshot.balance_vn2;
  const balanceBox = $('#ll-balance');
  balanceBox.textContent = fmtVn2(balance);
  balanceBox.className = 'kpi-value ' + (balance === null || balance === undefined ? '' : balance > 0 ? 'gold' : 'red');
  const balanceNote = $('#ll-balance-note');
  if (!snapshot.chrome_connected) {
    balanceNote.textContent = snapshot.chrome_note || 'Chrome chưa kết nối · bấm “Mở lại Chrome”.';
  } else {
    balanceNote.textContent = snapshot.read_error
      ? `lỗi đọc: ${snapshot.read_error}`
      : `${fmtVnd(balance === null || balance === undefined ? null : balance * 1000)} · cập nhật ${fmtTime(snapshot.read_at)}`;
  }

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

  const strip = $('#ll-ladder-strip');
  if (strip) {
    strip.innerHTML = ladder
      .map((value, index) => {
        const cls = index === snapshot.step ? 'now' : index < snapshot.step ? 'done' : '';
        return `<div class="ls-cell ${cls}"><b>${fmtUnits(value)}</b><small>bậc ${index + 1}</small></div>`;
      })
      .join('');
  }

  const chromeChip = $('#ll-chrome-chip');
  chromeChip.textContent = snapshot.chrome_connected ? 'Chrome: đã kết nối' : 'Chrome: chưa kết nối';
  chromeChip.dataset.state = snapshot.chrome_connected ? 'on' : 'off';
  const toolChip = $('#ll-tool-chip');
  toolChip.textContent = snapshot.tool_running ? 'Tool: đang chạy' : 'Tool: đang dừng';
  toolChip.dataset.state = snapshot.tool_running ? 'on' : 'warn';
  const toolButton = $('#ll-tool');
  toolButton.textContent = snapshot.tool_running ? 'Dừng tool' : 'Chạy tool';
  toolButton.classList.toggle('danger-btn', !!snapshot.tool_running);
  syncOpenChromeButton('#ll-open-chrome', 'longlong', snapshot);
  $('[data-led="longlong"]').classList.toggle('on', !!snapshot.tool_running || !!snapshot.chrome_connected);

  const config = snapshot.config || {};
  if (document.activeElement !== $('#ll-base')) $('#ll-base').value = config.base ?? '';
  if (document.activeElement !== $('#ll-capital')) $('#ll-capital').value = config.capital ?? '';
  if (document.activeElement !== $('#ll-min-gap')) $('#ll-min-gap').value = config.min_gap ?? 15;
  $('#ll-place-real').checked = !!config.place_real;
  $('#ll-auto-result').checked = config.auto_result !== false;

  // Cửa sổ Chrome + link mở đầu (giống tab Baccarat)
  const window = config.chrome_window || {};
  const full = $('#ll-fullscreen');
  if (full) full.checked = !!window.fullscreen;
  setValue('#ll-win-w', window.width);
  setValue('#ll-win-h', window.height);
  setValue('#ll-win-scale', window.percent);
  setValue('#ll-start-url', config.start_url);

  const edit = $('#ll-ladder-edit');
  const signature = ladder.join(',');
  if (edit && edit.dataset.signature !== signature) {
    edit.dataset.signature = signature;
    edit.innerHTML = ladder
      .map((value, index) => `<label>Bậc ${index + 1}<input data-ladder="${index}" type="number" min="1" step="10" value="${value}" /></label>`)
      .join('');
  }

  renderLongLongStats(snapshot);
}

/* ------------------------ LONG-LONG: thống kê hiệu quả ------------------------ */
function chartCanvas(canvas, minWidth = 240, minHeight = 150) {
  if (!canvas) return null;
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(minWidth, Math.round(rect.width || canvas.clientWidth || minWidth));
  const height = Math.max(minHeight, Math.round(rect.height || minHeight));
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

function chartText(ctx, text, x, y, size = 12, color = '#8ba0bd', weight = 600) {
  ctx.fillStyle = color;
  ctx.font = `${weight} ${size}px system-ui, -apple-system, "Segoe UI", sans-serif`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, x, y);
}

function drawRatioChart(canvas, stats) {
  const box = chartCanvas(canvas, 240, 170);
  if (!box) return;
  const { ctx, width, height } = box;
  const total = (stats.wins || 0) + (stats.losses || 0);
  const cx = width / 2;
  const cy = height / 2;
  const radius = Math.min(width, height) / 2 - 10;
  const thickness = Math.max(14, radius * 0.36);
  const arc = radius - thickness / 2;

  ctx.beginPath();
  ctx.arc(cx, cy, arc, 0, Math.PI * 2);
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
  ctx.lineWidth = thickness;
  ctx.stroke();
  if (!total) {
    chartText(ctx, 'Chưa có tay nào', cx, cy, 12);
    return;
  }
  let start = -Math.PI / 2;
  [{ value: stats.wins, color: '#34d399' }, { value: stats.losses, color: '#ff6b81' }]
    .forEach((part) => {
      const angle = (part.value / total) * Math.PI * 2;
      if (angle <= 0) return;
      ctx.beginPath();
      ctx.arc(cx, cy, arc, start, start + angle);
      ctx.strokeStyle = part.color;
      ctx.lineWidth = thickness;
      ctx.stroke();
      start += angle;
    });
  chartText(ctx, `${stats.win_rate ?? 0}%`, cx, cy - 7, 22, '#e8f1ff', 700);
  chartText(ctx, 'tỉ lệ thắng', cx, cy + 14, 11);
}

function drawStreakChart(canvas, stats) {
  const box = chartCanvas(canvas, 240, 170);
  if (!box) return;
  const { ctx, width, height } = box;
  const histogram = stats.streak_histogram || {};
  const lengths = Object.keys(histogram).map(Number).sort((a, b) => a - b);
  if (!lengths.length) {
    chartText(ctx, 'Chưa có chuỗi thua nào', width / 2, height / 2, 12);
    return;
  }
  const values = lengths.map((len) => histogram[String(len)]);
  const peak = Math.max(...values, 1);
  const padL = 26;
  const padR = 10;
  const padT = 14;
  const padB = 24;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;

  ctx.strokeStyle = 'rgba(255, 255, 255, 0.12)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padL, padT);
  ctx.lineTo(padL, padT + plotH);
  ctx.lineTo(padL + plotW, padT + plotH);
  ctx.stroke();

  const slot = plotW / lengths.length;
  const barW = Math.min(40, slot * 0.55);
  lengths.forEach((len, index) => {
    const x = padL + slot * (index + 0.5);
    const barH = (values[index] / peak) * plotH;
    const isMax = len === stats.max_loss_streak;
    ctx.fillStyle = isMax ? 'rgba(255, 107, 129, 0.85)' : 'rgba(255, 107, 129, 0.32)';
    ctx.fillRect(x - barW / 2, padT + plotH - barH, barW, barH);
    chartText(ctx, String(values[index]), x, padT + plotH - barH - 7, 10);
    chartText(ctx, `${len} tay`, x, padT + plotH + 13, 10);
  });
}

function drawCurveChart(canvas, stats) {
  const box = chartCanvas(canvas, 300, 150);
  if (!box) return;
  const { ctx, width, height } = box;
  const curve = stats.curve || [];
  const padL = 38;
  const padR = 12;
  const padT = 12;
  const padB = 16;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;
  if (curve.length < 2) {
    chartText(ctx, 'Cần ít nhất 2 tay để vẽ đường lãi/lỗ', width / 2, height / 2, 12);
    return;
  }
  const min = Math.min(0, ...curve);
  const max = Math.max(0, ...curve);
  const span = (max - min) || 1;
  const yOf = (value) => padT + plotH - ((value - min) / span) * plotH;
  const xOf = (index) => padL + (plotW / (curve.length - 1)) * index;

  ctx.strokeStyle = 'rgba(255, 255, 255, 0.16)';
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(padL, yOf(0));
  ctx.lineTo(padL + plotW, yOf(0));
  ctx.stroke();
  ctx.setLineDash([]);

  const last = curve[curve.length - 1];
  const colour = last >= 0 ? '#34d399' : '#ff6b81';
  ctx.beginPath();
  curve.forEach((value, index) => {
    const x = xOf(index);
    const y = yOf(value);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = colour;
  ctx.lineWidth = 2;
  ctx.stroke();

  ctx.lineTo(xOf(curve.length - 1), yOf(0));
  ctx.lineTo(xOf(0), yOf(0));
  ctx.closePath();
  ctx.fillStyle = last >= 0 ? 'rgba(52, 211, 153, 0.12)' : 'rgba(255, 107, 129, 0.12)';
  ctx.fill();

  ctx.textAlign = 'right';
  chartText(ctx, `${max.toFixed(0)}`, padL - 6, yOf(max) + 4, 10);
  chartText(ctx, `${min.toFixed(0)}`, padL - 6, yOf(min) - 4, 10);
}

function renderLongLongStats(snapshot) {
  if (!snapshot) return;
  const stats = snapshot.stats || {};
  const hands = stats.hands || 0;

  setText('#ll-stat-winrate', hands ? `${stats.win_rate}%` : '—');
  setText('#ll-stat-hands', `${hands} tay đã cược · ${snapshot.hands_seen || 0} tay đã xem`);
  setText('#ll-stat-wl', `${stats.wins || 0} / ${stats.losses || 0}`);
  setText('#ll-stat-lossrate', hands ? `thua ${stats.loss_rate}%` : 'chưa có dữ liệu');
  setText('#ll-stat-maxloss', hands ? `${stats.max_loss_streak} tay` : '—');
  setText('#ll-stat-currentloss', `hiện tại: ${stats.current_loss_streak || 0} tay · thắng liền ${stats.max_win_streak || 0}`);

  const net = Number(stats.net_units || 0);
  const netBox = $('#ll-stat-net');
  if (netBox) {
    netBox.textContent = hands ? `${net > 0 ? '+' : ''}${fmtUnits(net, 2)} đv` : '—';
    netBox.className = 'kpi-value ' + (net > 0 ? 'green' : net < 0 ? 'red' : '');
  }
  setText('#ll-stat-worst', hands ? `tay lỗ nặng nhất: ${fmtUnits(stats.worst_loss, 2)} đv` : 'tay lỗ nặng nhất: —');

  const legend = $('#ll-legend-ratio');
  if (legend) {
    legend.innerHTML = [
      `<span class="legend-item"><i class="legend-dot" style="background:#34d399"></i>Thắng ${stats.wins || 0} (${hands ? stats.win_rate : 0}%)</span>`,
      `<span class="legend-item"><i class="legend-dot" style="background:#ff6b81"></i>Thua ${stats.losses || 0} (${hands ? stats.loss_rate : 0}%)</span>`,
    ].join('');
  }

  const streakNote = $('#ll-streak-note');
  if (streakNote) {
    streakNote.textContent = hands
      ? `Cột đỏ đậm = chuỗi thua dài nhất (${stats.max_loss_streak} tay). `
        + `Chuỗi ngắn (1–2 tay) nhiều là bình thường; chuỗi ≥ 4 tay lặp lại nghĩa là thang cược đang bị thử thách.`
      : 'Chưa có tay nào được cược nên chưa có chuỗi thua.';
  }

  const recent = $('#ll-recent');
  if (recent) {
    recent.innerHTML = (stats.recent || []).length
      ? stats.recent.map((flag) => `<span class="wl-cell ${flag === 'W' ? 'w' : 'l'}">${flag}</span>`).join('')
      : '<span class="hint">Chưa có tay nào.</span>';
  }

  // Canvas chỉ vẽ khi tab đang hiện (kích thước thật khác 0).
  const panel = $('.subview[data-game="longlong"][data-sub="stats"]');
  if (!panel || panel.classList.contains('hidden')) return;
  drawRatioChart($('#ll-chart-ratio'), stats);
  drawStreakChart($('#ll-chart-streak'), stats);
  drawCurveChart($('#ll-chart-curve'), stats);
}

/* =============================== BACCARAT =============================== */
const CHIP_STATUS = {
  match: ['KHỚP', 'ok'],
  no_bet: ['CHƯA ĐẶT', 'warn'],
  wrong_side: ['SAI CỬA', 'bad'],
  wrong_amount: ['SAI TIỀN', 'bad'],
  extra_bets: ['ĐẶT THÊM', 'bad'],
  ungroupable: ['KHÔNG GHÉP ĐƯỢC', 'bad'],
  unknown: ['CHƯA ĐỌC', 'warn'],
};

function setText(selector, text) {
  const node = $(selector);
  if (node) node.textContent = text;
}

function setValue(selector, value) {
  const node = $(selector);
  if (node && document.activeElement !== node) node.value = value ?? '';
}

function renderRoad(letters) {
  const box = $('#ba-road');
  if (!box) return;
  const text = String(letters || '');
  if (!text) {
    box.innerHTML = '<span class="hint">Chưa có cầu.</span>';
    return;
  }
  box.innerHTML = text
    .split('')
    .slice(-60)
    .map((ch) => `<span class="road-cell ${ch.toLowerCase()}">${ch}</span>`)
    .join('');
}

function renderBlockStrip(table, plan) {
  const box = $('#ba-block-strip');
  if (!box) return;
  const cells = Number(table.cycle_hands || 0);
  if (!cells) { box.innerHTML = ''; return; }
  const strongHands = Number(table.strong_hands || 0);
  const strong = plan.strong || table.strong || null;
  const weak = strong === 'B' ? 'P' : strong === 'P' ? 'B' : null;
  const pos = Number(plan.block_pos || 0);
  let html = '';
  for (let i = 0; i < cells; i += 1) {
    const side = i < strongHands ? strong : weak;
    const active = i === pos && plan.action === 'bet';
    const cls = ['ps-cell', `side-${(side || '').toLowerCase() || 'none'}`, active ? 'now' : ''].join(' ');
    html += `<div class="${cls}"><b>${side || '·'}</b><small>tay ${i + 1}</small></div>`;
  }
  box.innerHTML = html;
}

function renderChecks(plan) {
  const box = $('#ba-checks');
  if (!box) return;
  const checks = plan.checks || [];
  if (!checks.length) { box.innerHTML = ''; return; }
  box.innerHTML = checks
    .map((check) => `<div class="check-line ${check.ok ? 'ok' : 'bad'}">
      <span class="check-mark">${check.ok ? '✓' : '•'}</span>
      <span>${escapeHtml(check.ok ? check.name : check.detail)}</span></div>`)
    .join('');
}

function renderChips(chips, plan) {
  const statusBox = $('#ba-chip-status');
  const detailBox = $('#ba-chip-detail');
  if (!statusBox || !detailBox) return;
  const check = chips && chips.check;
  const live = chips && chips.live;
  if (!check) {
    statusBox.textContent = 'Chưa đọc chip.';
    statusBox.dataset.state = 'warn';
    detailBox.textContent = 'Vào bàn rồi bấm “Đọc lại bàn” để đọc chip đang đặt.';
    return;
  }
  const [label, level] = CHIP_STATUS[check.status] || [check.status, 'warn'];
  statusBox.textContent = `${label} · ${check.note || ''}`;
  statusBox.dataset.state = level;

  const lines = [];
  if (check.suggested_chips && check.suggested_chips.length && plan.side) {
    lines.push(`Cần bấm: ${check.suggested_chips.join(' + ')} (${check.suggested_chips.length} chip) = ${fmtMoney(check.expected)}`);
  } else if (check.expected) {
    lines.push(`Kế hoạch: ${fmtMoney(check.expected)}`);
  }
  if (live && live.ok) {
    const parts = [];
    ['B', 'P', 'T'].forEach((side) => {
      const spot = (live.spots || {})[side] || {};
      if (spot.amount_vnd) {
        const chipText = (spot.chips || []).join(' + ');
        parts.push(`${SIDE_NAME[side]} ${fmtMoney(spot.amount_vnd)}${chipText ? ` [${chipText}]` : ''}`);
      }
    });
    lines.push(`Trên bàn: ${parts.length ? parts.join(' · ') : 'chưa có chip'}`);
    if (live.total !== null && live.total !== undefined) {
      lines.push(`Tổng đặt: ${fmtMoney(live.total)}`);
    }
  } else if (live && live.error) {
    lines.push(`Chưa đọc được chip (${live.error}).`);
  }
  detailBox.innerHTML = lines.map((line) => `<div>${escapeHtml(line)}</div>`).join('');
}

function renderTables(rows) {
  const box = $('#ba-tables');
  const title = $('#ba-table-title');
  if (!box) return;
  const search = state.tableSearch.trim().toLowerCase();
  const list = (rows || []).filter((row) => {
    if (state.onlyEligible && !row.eligible) return false;
    return !search || String(row.label || '').toLowerCase().includes(search);
  });
  if (title) title.textContent = `Bàn đọc được · ${list.length}`;
  if (!list.length) {
    box.innerHTML = '<p class="hint">Không có bàn nào khớp. Mở Chrome, vào sảnh Baccarat rồi bấm “Quét sảnh”.</p>';
    return;
  }
  box.innerHTML = list
    .map((row) => {
      const tagClass = row.read_error ? 'bad' : row.ready ? 'ok' : row.eligible ? 'warn' : 'muted';
      const actions = [
        `<button class="btn tiny" data-act="select" data-key="${escapeHtml(row.key)}">Theo dõi</button>`,
        row.table_id ? `<button class="btn tiny" data-act="open" data-key="${escapeHtml(row.key)}">Mở bàn</button>` : '',
      ].join('');
      const tieText = row.tie_side === 'B' ? 'đỏ' : row.tie_side === 'P' ? 'xanh' : 'vệt';
      const leadText = row.lead === null || row.lead === undefined
        ? ''
        : ` · B ${row.banker} / P ${row.player} · lệch ${row.lead}${row.tie_break ? ` (hoà → ${tieText})` : ''}`;
      return `<div class="tbl-row ${row.current ? 'current' : ''}">
        <div class="tbl-main">
          <b>${escapeHtml(row.label || row.key)}</b>
          <small>${row.hands} tay${leadText} · mạnh ${escapeHtml(row.strong_name || '—')}${row.current ? ' · đang theo dõi' : ''}</small>
        </div>
        <div class="tbl-meta">
          <span class="tag ${tagClass}">${escapeHtml(row.verdict || '')}</span>
          <small>${escapeHtml(row.status || '')}</small>
        </div>
        <div class="tbl-actions">${actions}</div>
      </div>`;
    })
    .join('');
}

function renderHistory(history, money) {
  const body = $('#ba-history-body');
  const note = $('#ba-history-note');
  if (!body) return;
  const rows = (history || []).slice().reverse();
  if (note) {
    note.textContent = `${money.hands || 0} tay · ${money.wins || 0} thắng / ${money.losses || 0} thua / `
      + `${money.pushes || 0} hoà · lãi/lỗ ${fmtSigned(money.profit)} · sụt vốn lớn nhất ${fmtMoney(money.drawdown)}`;
  }
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">Chưa có tay nào được ghi.</td></tr>';
    return;
  }
  body.innerHTML = rows
    .map((row) => {
      const cls = row.outcome === 'win' ? 'win' : row.outcome === 'loss' ? 'loss' : row.outcome === 'not_placed' ? 'warn' : '';
      const result = row.result ? `${row.result}` : '—';
      const bet = row.bet ? SIDE_NAME[row.bet] || row.bet : '—';
      return `<tr class="${cls}">
        <td>${escapeHtml(row.at || '')}</td>
        <td>${escapeHtml(row.table || '')}</td>
        <td>${row.hand || ''}</td>
        <td>${escapeHtml(bet)}</td>
        <td>${escapeHtml(result)}</td>
        <td>${row.stake ? fmtMoney(row.stake) : '—'}</td>
        <td>${row.delta ? fmtSigned(row.delta) : '—'}</td>
        <td>${fmtMoney(row.balance_after)}</td>
      </tr>`;
    })
    .join('');
}

function renderBaLadder(config) {
  const box = $('#ba-ladder-edit');
  if (!box) return;
  const ladder = config.ladder || [];
  const signature = `${config.profile}|${ladder.join(',')}`;
  if (box.dataset.signature === signature) return;
  box.dataset.signature = signature;
  const readonly = config.ladder_mode === 'multiply' ? 'readonly' : '';
  box.innerHTML = ladder
    .map((value, index) => `<label>Bậc ${index + 1}<input data-ba-ladder="${index}" type="number" min="1" step="10" value="${value}" ${readonly} /></label>`)
    .join('');
}

function renderBaConfig(config) {
  if (!config) return;
  setValue('#ba-capital-input', config.capital);
  setValue('#ba-tp-input', config.take_profit_pct);
  setValue('#ba-min-hands', config.min_hands);
  setValue('#ba-max-hands', config.max_hands);
  setValue('#ba-max-streak', config.max_streak);
  setValue('#ba-min-lead', config.min_lead);
  setValue('#ba-max-lead', config.max_lead);
  setValue('#ba-live-exit', config.live_exit_streak);
  setValue('#ba-tables-target', config.tables_to_play);
  setValue('#ba-ladder-base', config.ladder_base);
  setValue('#ba-start-url', config.start_url);

  $$('input[name="ba-profile"]').forEach((input) => { input.checked = input.value === config.profile; });
  $$('input[name="ba-block-mode"]').forEach((input) => { input.checked = input.value === config.block_mode; });
  $$('input[name="ba-ladder-mode"]').forEach((input) => { input.checked = input.value === config.ladder_mode; });
  $$('input[name="ba-tie-side"]').forEach((input) => { input.checked = input.value === (config.tie_side || 'B'); });

  const flags = {
    '#ba-auto-cycle': config.auto_cycle,
    '#ba-auto-hunt': config.auto_hunt,
    '#ba-auto-recover': config.auto_recover,
    '#ba-favorites-only': config.favorites_only,
    '#ba-human-mouse': config.human_mouse,
    '#ba-auto-place': config.auto_place,
  };
  Object.entries(flags).forEach(([selector, value]) => {
    const node = $(selector);
    if (node) node.checked = !!value;
  });

  const window = config.chrome_window || {};
  const full = $('#ba-fullscreen');
  if (full) full.checked = !!window.fullscreen;
  setValue('#ba-win-w', window.width);
  setValue('#ba-win-h', window.height);
  setValue('#ba-win-scale', window.percent);

  renderBaLadder(config);
}

function renderBaccarat(snapshot) {
  if (!snapshot) return;
  state.baccarat = snapshot;
  const money = snapshot.money || {};
  const table = snapshot.table || {};
  const plan = snapshot.plan || {};
  const config = snapshot.config || {};

  /* --- KPI --- */
  const capitalBox = $('#ba-capital');
  if (capitalBox) {
    capitalBox.textContent = fmtMoney(money.capital);
    capitalBox.className = 'kpi-value';
  }
  setText('#ba-capital-note', `Số dư phiên: ${fmtMoney(money.balance)}`);

  const profitBox = $('#ba-profit');
  if (profitBox) {
    profitBox.textContent = fmtSigned(money.profit);
    profitBox.className = 'kpi-value ' + (money.profit > 0 ? 'green' : money.profit < 0 ? 'red' : '');
  }
  setText('#ba-profit-note', `${money.hands || 0} tay · ${money.wins || 0}T/${money.losses || 0}B/${money.pushes || 0}H`);

  const roiBox = $('#ba-roi');
  if (roiBox) {
    roiBox.textContent = `${money.roi > 0 ? '+' : ''}${pct(money.roi)}`;
    roiBox.className = 'kpi-value ' + (money.roi > 0 ? 'green' : money.roi < 0 ? 'red' : '');
  }
  setText('#ba-roi-note', `sụt vốn ${fmtMoney(money.drawdown)}`);

  setText('#ba-winrate', `${Math.round(money.win_rate || 0)}% / ${Math.round(100 - (money.win_rate || 0))}%`);
  setText('#ba-winrate-note', money.blocked ? String(money.blocked[1] || 'đã chặn') : 'không tính hoà');

  /* --- thanh trạng thái --- */
  const chromeChip = $('#ba-chrome-chip');
  if (chromeChip) {
    chromeChip.textContent = snapshot.chrome_connected ? 'Chrome: đã kết nối' : 'Chrome: chưa kết nối';
    chromeChip.dataset.state = snapshot.chrome_connected ? 'on' : 'off';
  }
  const toolChip = $('#ba-tool-chip');
  if (toolChip) {
    toolChip.textContent = snapshot.tool_running ? 'Tool: đang chạy' : 'Tool: đang dừng';
    toolChip.dataset.state = snapshot.tool_running ? 'on' : 'warn';
  }
  const toolButton = $('#ba-tool');
  if (toolButton) {
    toolButton.textContent = snapshot.tool_running ? 'Dừng tool' : 'Chạy tool';
    toolButton.classList.toggle('danger-btn', !!snapshot.tool_running);
  }
  syncOpenChromeButton('#ba-open-chrome', 'baccarat', snapshot);
  const led = $('[data-led="baccarat"]');
  if (led) led.classList.toggle('on', !!snapshot.tool_running || !!snapshot.chrome_connected);

  const busy = snapshot.busy || {};
  ['#ba-attach', '#ba-lobby-enter', '#ba-sweep', '#ba-scan', '#ba-leave', '#ba-close'].forEach((selector) => {
    const node = $(selector);
    if (node) node.disabled = !!busy.chrome && selector !== '#ba-close';
  });

  setText('#ba-conn-note', snapshot.chrome_note || '');
  setText('#ba-wallet', snapshot.wallet_text || '');
  setText('#ba-sweep-note', snapshot.sweep_note || '');

  /* --- banner tự săn bàn --- */
  const hunt = $('#ba-hunt');
  const cycle = snapshot.cycle || {};
  if (hunt) {
    if (cycle.waiting) {
      hunt.classList.remove('hidden');
      state.huntTick += 1;
      const spin = $('#ba-hunt-spin');
      if (spin) spin.textContent = state.huntTick % 2 ? '●' : '○';
      const remain = Math.max(0, Math.round(cycle.retry_in || 0));
      setText('#ba-hunt-text', remain > 0
        ? `Đang tự săn bàn… nghỉ ${remain}s rồi quét lại (đã thử ${cycle.attempts || 0} lượt).`
        : `Đang tự quét tìm bàn đạt chuẩn… (đã thử ${cycle.attempts || 0} lượt)`);
    } else {
      hunt.classList.add('hidden');
    }
  }

  /* --- kế hoạch tay kế tiếp --- */
  const planState = $('#ba-plan-state');
  const planBox = $('#ba-plan');
  const planReason = $('#ba-plan-reason');
  if (planState && planBox) {
    if (!table.key) {
      planState.textContent = 'CHƯA CÓ BÀN';
      planState.className = 'hero-state';
      planBox.textContent = 'Chọn một bàn trong danh sách rồi bấm “Theo dõi”.';
    } else if (money.blocked) {
      planState.textContent = 'ĐÃ CHẶN';
      planState.className = 'hero-state stop';
      planBox.textContent = String(money.blocked[1] || 'Phiên đã chạm ngưỡng dừng.');
    } else if (plan.action === 'bet') {
      planState.textContent = plan.armed ? 'ĐÃ CHỐT' : 'ĐẶT';
      planState.className = 'hero-state bet';
      planBox.textContent = `${plan.side_name || SIDE_NAME[plan.side] || '—'} ${fmtMoney(plan.stake)} · bậc ${(plan.ladder_step || 0) + 1}`
        + `/ ${(table.ladder || []).length}`;
    } else if (plan.action === 'halt') {
      planState.textContent = 'DỪNG';
      planState.className = 'hero-state stop';
      planBox.textContent = plan.reason || 'Đã thua hết thang.';
    } else if (plan.action === 'start') {
      planState.textContent = 'BẮT ĐẦU';
      planState.className = 'hero-state bet';
      planBox.textContent = `Vào khối ${table.profile || ''} · ${plan.side_name || ''} ${fmtMoney(plan.stake)}`;
    } else {
      planState.textContent = 'CHỜ';
      planState.className = 'hero-state';
      planBox.textContent = table.running ? 'Chờ tay mới để chốt kế hoạch.' : 'Chưa theo dõi bàn.';
    }
    if (planReason) planReason.textContent = plan.reason || '';
  }
  renderBlockStrip(table, plan);
  renderChecks(plan);

  /* --- bàn đang theo dõi --- */
  setText('#ba-table-name', table.key ? `${table.name || table.key} · ${table.hands || 0} tay` : 'Chưa chọn bàn');
  renderRoad(table.letters);
  const noteBits = [];
  if (table.key) {
    noteBits.push(table.running ? 'Đang theo dõi' : 'Đã dừng');
    noteBits.push(`khối ${table.profile_label || table.profile || ''}`);
    noteBits.push(`bậc ${Number(plan.ladder_step || 0) + 1}/${(table.ladder || []).length}`);
    if (table.entry_waiting) noteBits.push('chờ kết quả tay hiện tại');
    if (table.place_skips) noteBits.push(`bỏ ${table.place_skips} tay`);
  } else {
    noteBits.push('Chọn một bàn trong danh sách bên dưới.');
  }
  setText('#ba-table-note', [table.note, noteBits.join(' · ')].filter(Boolean).join(' — '));

  renderChips(snapshot.chips, plan);
  const place = snapshot.place || {};
  const placeBox = $('#ba-place-status');
  if (placeBox) {
    placeBox.textContent = place.note || '';
    placeBox.dataset.state = place.ok === true ? 'on' : place.ok === false ? 'err' : '';
  }

  renderTables(snapshot.tables || []);
  renderBaConfig(config);
  renderHistory(snapshot.history || [], money);
}

/* ============================== sự kiện chung ============================== */
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

/* ------------------------------ LONG-LONG actions ------------------------------ */
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

$('#ll-save-window').addEventListener('click', async () => {
  const reply = await call('longlong', 'chrome.window', {
    width: Number($('#ll-win-w').value),
    height: Number($('#ll-win-h').value),
    percent: Number($('#ll-win-scale').value),
    fullscreen: $('#ll-fullscreen').checked,
  });
  if (reply) toast(reply.note || 'Đã lưu cửa sổ Chrome.');
});

$('#ll-save-url').addEventListener('click', async () => {
  const reply = await call('longlong', 'config.set', {
    start_url: $('#ll-start-url').value.trim(),
  });
  if (reply) toast('Đã lưu link mở đầu cho LONG-LONG.');
});

$('#ll-clear-log').addEventListener('click', () => {
  state.logs.longlong = [];
  renderLog('longlong');
});

/* ------------------------------ BACCARAT actions ------------------------------ */
$('#ba-open-chrome').addEventListener('click', async () => {
  await call('baccarat', 'chrome.open');
  toast('Đang mở Chrome cho Baccarat — đăng nhập rồi vào sảnh.');
});

$('#ba-attach').addEventListener('click', async () => {
  await call('baccarat', 'chrome.attach');
  toast('Đang gắn Chrome đang mở (CDP 9222).');
});

$('#ba-lobby-enter').addEventListener('click', async () => {
  const reply = await call('baccarat', 'lobby.enter');
  if (reply) toast(reply.note || 'Đang tìm tab sảnh…');
});

$('#ba-sweep').addEventListener('click', async () => {
  const reply = await call('baccarat', 'lobby.sweep', { favorites_only: $('#ba-favorites-only').checked });
  if (reply) toast(reply.note || 'Đang quét sảnh…');
});

$('#ba-scan').addEventListener('click', async () => {
  const reply = await call('baccarat', 'scan');
  if (reply) toast(reply.note || 'Đang đọc lại bàn…');
});

$('#ba-leave').addEventListener('click', async () => {
  const reply = await call('baccarat', 'table.leave', { take_profit: false });
  if (reply) toast(reply.note || 'Đang rời bàn…');
});

$('#ba-close').addEventListener('click', async () => {
  await call('baccarat', 'chrome.close');
  toast('Đang ngắt kết nối Chrome.');
});

$('#ba-tool').addEventListener('click', async () => {
  const running = state.baccarat && state.baccarat.tool_running;
  const reply = await call('baccarat', running ? 'tool.stop' : 'tool.start');
  if (reply) toast(reply.note || (running ? 'Đã dừng tool.' : 'Tool đang chạy.'));
});

$('#ba-hunt-cancel').addEventListener('click', async () => {
  await call('baccarat', 'config.set', { auto_hunt: false });
  toast('Đã dừng tự săn bàn.');
});

$('#ba-search').addEventListener('input', (event) => {
  state.tableSearch = event.target.value || '';
  renderTables((state.baccarat && state.baccarat.tables) || []);
});

$('#ba-only-eligible').addEventListener('change', (event) => {
  state.onlyEligible = !!event.target.checked;
  renderTables((state.baccarat && state.baccarat.tables) || []);
});

$('#ba-tables').addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-act]');
  if (!button) return;
  const key = button.dataset.key;
  button.disabled = true;
  try {
    if (button.dataset.act === 'open') {
      const reply = await call('baccarat', 'table.open', { key });
      if (reply) toast(reply.note || 'Đang mở bàn…');
    } else {
      const reply = await call('baccarat', 'table.select', { key });
      if (reply) toast(reply.note || 'Đã chọn bàn.');
    }
  } finally {
    button.disabled = false;
  }
});

$('#ba-save-money').addEventListener('click', async () => {
  const reply = await call('baccarat', 'config.set', {
    capital: Number($('#ba-capital-input').value),
    take_profit_pct: Number($('#ba-tp-input').value),
  });
  if (reply) toast(reply.note || 'Đã lưu vốn.');
});

$('#ba-new-session').addEventListener('click', async () => {
  const reply = await call('baccarat', 'session.new', {
    capital: Number($('#ba-capital-input').value),
    take_profit_pct: Number($('#ba-tp-input').value),
  });
  if (reply) toast(reply.note || 'Đã tạo phiên mới.');
});

$('#ba-reset-session').addEventListener('click', async () => {
  const reply = await call('baccarat', 'session.reset');
  if (reply) toast(reply.note || 'Đã reset phiên.');
});

$('#ba-save-ladder').addEventListener('click', async () => {
  const profile = ($('input[name="ba-profile"]:checked') || {}).value || '3-3';
  const blockMode = ($('input[name="ba-block-mode"]:checked') || {}).value || 'recalculate';
  const ladderMode = ($('input[name="ba-ladder-mode"]:checked') || {}).value || 'manual';
  const ladder = $$('#ba-ladder-edit input').map((input) => Number(input.value)).filter((value) => value > 0);
  const params = {
    profile,
    block_mode: blockMode,
    ladder_mode: ladderMode,
    ladder_base: Number($('#ba-ladder-base').value),
  };
  if (ladderMode === 'manual' && ladder.length) params.ladder = ladder;
  const reply = await call('baccarat', 'config.set', params);
  if (reply) toast(reply.note || 'Đã lưu phương pháp & thang.');
});

$('#ba-save-filter').addEventListener('click', async () => {
  const reply = await call('baccarat', 'config.set', {
    min_hands: Number($('#ba-min-hands').value),
    max_hands: Number($('#ba-max-hands').value),
    max_streak: Number($('#ba-max-streak').value),
    min_lead: Number($('#ba-min-lead').value),
    max_lead: Number($('#ba-max-lead').value),
    live_exit_streak: Number($('#ba-live-exit').value),
    tie_side: ($('input[name="ba-tie-side"]:checked') || {}).value || 'B',
  });
  if (reply) toast(reply.note || 'Đã lưu bộ lọc cầu.');
});

$('#ba-save-options').addEventListener('click', async () => {
  const reply = await call('baccarat', 'config.set', {
    auto_cycle: $('#ba-auto-cycle').checked,
    auto_hunt: $('#ba-auto-hunt').checked,
    auto_recover: $('#ba-auto-recover').checked,
    favorites_only: $('#ba-favorites-only').checked,
    human_mouse: $('#ba-human-mouse').checked,
    auto_place: $('#ba-auto-place').checked,
    tables_to_play: Number($('#ba-tables-target').value),
  });
  if (reply) toast(reply.note || 'Đã lưu tùy chọn.');
});

$('#ba-save-window').addEventListener('click', async () => {
  const reply = await call('baccarat', 'chrome.window', {
    width: Number($('#ba-win-w').value),
    height: Number($('#ba-win-h').value),
    percent: Number($('#ba-win-scale').value),
    fullscreen: $('#ba-fullscreen').checked,
  });
  if (reply) toast(reply.note || 'Đã lưu cửa sổ Chrome.');
});

$('#ba-save-url').addEventListener('click', async () => {
  const reply = await call('baccarat', 'config.set', { start_url: $('#ba-start-url').value.trim() });
  if (reply) toast(reply.note || 'Đã lưu link.');
});

$('#ba-export').addEventListener('click', async () => {
  const snap = state.baccarat;
  const rows = (snap && snap.history) || [];
  if (!rows.length) {
    toast('Chưa có dữ liệu để xuất.');
    return;
  }
  const head = ['time', 'table', 'hand', 'bet', 'result', 'stake', 'delta', 'outcome', 'balance_after'];
  const csv = [head.join(',')]
    .concat(rows.map((row) => head.map((key) => {
      const value = row[key === 'time' ? 'at' : key];
      const text = value === null || value === undefined ? '' : String(value);
      return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    }).join(',')))
    .join('\n');
  const name = `bcr-baccarat-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.csv`;
  const saved = await api.saveText(name, csv);
  if (saved && saved.ok) toast(`Đã lưu: ${saved.path}`);
  else if (saved && saved.error) toast(saved.error);
});

$('#ba-clear-log').addEventListener('click', () => {
  state.logs.baccarat = [];
  renderLog('baccarat');
});

/* ---------------------------------- khởi động ---------------------------------- */
let chartResizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(chartResizeTimer);
  chartResizeTimer = setTimeout(() => {
    if (state.longlong && state.sub.longlong === 'stats') renderLongLongStats(state.longlong);
  }, 150);
});

(async function boot() {
  renderLog('longlong');
  renderLog('baccarat');
  const info = await api.info();
  $('#app-ver').textContent = `v${info.version}`;
  switchGame('baccarat');
  const ba = await call('baccarat', 'state');
  if (ba) renderBaccarat(ba);
  const ll = await call('longlong', 'state');
  if (ll) renderLongLong(ll);
})();
