const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(__dirname + '/extension/reader.js', 'utf8');

function canvas(history, {id = 'tableABC', title = 'Baccarat Tốc Độ A', type = 'baccarat', stale = false} = {}) {
  const root = {stateNode: {}};
  root.stateNode.current = root;
  const table = {memoizedProps: {table: {id, title}}, return: root};
  const game = {memoizedProps: {gameType: type}, return: table};
  const road = {memoizedProps: {type: 'BaccaratHistoryRoadTypeBig', history}, return: game};
  const current = {memoizedProps: {}, return: road};
  const oldRoot = {stateNode: {current: root}};
  const old = {memoizedProps: {}, return: oldRoot, alternate: current};
  const element = {__reactFiber$test: stale ? old : current,
    getBoundingClientRect: () => ({x: 10, y: 20, width: 250, height: 48})};
  root.child = table;
  table.child = game;
  game.child = road;
  road.child = current;
  current.stateNode = element;
  return element;
}

function scan(canvases) {
  const ctx = {window: {}, location: {href: 'https://tmrw17k36as.evo-games.com/frontend/evo/r3/?token=not-output#category=all_games',
    origin: 'https://tmrw17k36as.evo-games.com', pathname: '/frontend/evo/r3/'},
    document: {title: 'Lobby', querySelector: () => ({}), querySelectorAll: selector => {
      assert.equal(selector, '[data-role="history-roads"] canvas');
      return canvases;
    }}};
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return JSON.parse(JSON.stringify(ctx.window.__BCR_SCAN()));
}

const history = [{pos: [0, 0], c: 'R'}, {pos: [1, 0], c: 'B', ties: 2}, {pos: [2, 0], c: 'R'}];

test('uses result array order, red=Banker, blue=Player, includes ties', () => {
  const result = scan([canvas(history)]);
  assert.equal(result.tables[0].letters, 'BPTTB');
  assert.equal(result.tables[0].label, 'Baccarat Tốc Độ A');
  assert.equal(result.tables[0].table_id, 'tableABC');
  assert.equal(result.tables[0].read_error, '');
  assert.equal(result.tables[0].source, 'evo-canvas-big');
  assert.equal(result.diag.kind, 'evo-canvas');
  assert.ok(!JSON.stringify(result).includes('not-output'));
});

test('reads committed alternate fiber rather than stale branch', () => {
  assert.equal(scan([canvas(history, {stale: true})]).tables[0].letters, 'BPTTB');
});

test('ignores stale return ancestry even when it reaches the current root', () => {
  const element = canvas(history);
  const current = element.__reactFiber$test;
  const root = current.return.return.return.return;
  element.__reactFiber$test = {return: {memoizedProps: {
    type: 'BaccaratHistoryRoadTypeBig', history: history.slice(0, 1)
  }, return: {memoizedProps: {table: {id: 'wrong', title: 'Old'}}, return: root}}};
  assert.equal(scan([element]).tables[0].letters, 'BPTTB');
  assert.equal(scan([element]).tables[0].table_id, 'tableABC');
});

test('blocks known special-payout variants even when gameType says baccarat', () => {
  for (const id of ['LightningBac0001', 'XXXtremeLB000001', 'NoCommBac0000001',
    'gwbaccarat000001', 'Empgwbaccarat001', 'PTBaccarat000001', 'InsuranceBac0001']) {
    const row = scan([canvas(history, {id})]).tables[0];
    assert.ok(row.read_error.includes('trả thưởng đặc biệt'));
    assert.equal(row.letters, 'BPTTB');
  }
  const element = canvas(history);
  element.__reactFiber$test.return.return.return.memoizedProps.table.gameDetails = {gameConfig: {lightning: true}};
  assert.ok(scan([element]).tables[0].read_error.includes('trả thưởng đặc biệt'));
});

test('keeps IDs distinct when translated table names match', () => {
  const result = scan([canvas(history), canvas(history, {id: 'tableXYZ'})]);
  assert.equal(result.tables.length, 2);
  assert.notEqual(result.tables[0].table_id, result.tables[1].table_id);
});

test('marks first-cell ties as ambiguous rather than claiming exact history', () => {
  const row = scan([canvas([{pos: [0, 0], c: 'R', ties: 1}])]).tables[0];
  assert.ok(row.read_error.includes('Ô đầu có hòa'));
});

test('rejects truncated first column and unknown colors', () => {
  assert.ok(scan([canvas([{pos: [4, 0], c: 'R'}])]).tables[0].read_error);
  const row = scan([canvas([{pos: [0, 0], c: 'G'}])]).tables[0];
  assert.equal(row.letters, '');
  assert.ok(row.read_error);
});

test('rejects invalid ties and oversized road', () => {
  for (const ties of [-1, 1.5, 51, '2']) {
    const row = scan([canvas([{pos: [0, 0], c: 'R', ties}])]).tables[0];
    assert.equal(row.letters, '');
    assert.ok(row.read_error);
  }
  const row = scan([canvas(Array.from({length: 401}, (_, i) => ({pos: [i, 0], c: 'R'})))]).tables[0];
  assert.equal(row.letters, '');
  assert.ok(row.read_error);
});

test('exposes new shoes and Lucky metadata but ignores unrelated games', () => {
  const result = scan([canvas([]), canvas(history, {id: 'lucky', type: 'luckybaccarat'}),
    canvas(history, {id: 'roulette', type: 'roulette'})]);
  assert.equal(result.tables.length, 2);
  assert.equal(result.tables[0].letters, '');
  assert.equal(result.tables[1].game_type, 'luckybaccarat');
});

test('does not fall back to unrelated SVG shapes in the lobby', () => {
  assert.equal(scan([]).tables.length, 0);
});

function beadRoad(names) {
  return {
    getBoundingClientRect: () => ({x: 5, y: 7, width: 320, height: 260}),
    querySelectorAll: selector => {
      assert.equal(selector, '[data-role="roadItem"][name]');
      return names.map(name => ({getAttribute: key => {
        assert.equal(key, 'name');
        return name;
      }}));
    },
  };
}

function scanTableScreen(beads, href = 'https://tmrw17k36as.evo-games.com/frontend/evo/r3/#category=all_games&table_id=suropcmnbqpabwko') {
  const ctx = {window: {}, location: {href, origin: 'https://tmrw17k36as.evo-games.com', pathname: '/frontend/evo/r3/'},
    document: {title: 'Baccarat', querySelector: selector => {
      assert.equal(selector, '[data-role="table-tile"]');
      return null;
    }, querySelectorAll: selector => {
      assert.ok(['*', 'svg circle, svg ellipse, svg rect, svg path, svg use',
        '[data-role="history-roads"] canvas', '[data-role="Bead-road"]'].includes(selector), selector);
      return selector === '[data-role="Bead-road"]' ? beads : [];
    }}};
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return JSON.parse(JSON.stringify(ctx.window.__BCR_SCAN()));
}

test('reads the opened-table bead road and ignores pair badges after the result', () => {
  const result = scanTableScreen([beadRoad(['Banker', 'Banker Player', 'Player', 'Player Banker', 'Tie'])]);
  assert.equal(result.diag.kind, 'evo-bead');
  assert.deepEqual(result.tables.map(r => r.letters), ['BBPPT']);
  assert.equal(result.tables[0].read_error, '');
  assert.equal(result.tables[0].source, 'evo-bead');
  // Tên bàn không có trong DOM: nhãn tạm là mã bàn đọc từ URL, browser.py gắn table_id thật.
  assert.equal(result.tables[0].label, 'suropcmnbqpabwko');
  assert.equal(result.tables[0].table_id, '');
});

test('rejects unknown bead symbols and oversized table roads', () => {
  const odd = scanTableScreen([beadRoad(['Banker', 'Dragon'])]).tables[0];
  assert.equal(odd.letters, '');
  assert.ok(odd.read_error.includes('ký hiệu lạ'));
  const long = scanTableScreen([beadRoad(Array.from({length: 401}, () => 'Banker'))]).tables[0];
  assert.equal(long.letters, '');
  assert.ok(long.read_error.includes('400 tay'));
});

test('skips bead containers without readable items', () => {
  assert.equal(scanTableScreen([beadRoad([])]).tables.length, 0);
});

// EVO_ENTRY_JS song song với luồng bấm nút ở lobby_stream.py: doc truc tiep tu nguồn Python.
const entrySource = /EVO_ENTRY_JS = """([\s\S]*?)"""/.exec(
  fs.readFileSync(__dirname + '/lobby_stream.py', 'utf8'))[1];

function evoItem({attrs = {'data-subprovider': 'EVO'}, alt = 'Evolution', disabled = false,
                  buttonClass = 'nrc-button btn-main', buttonText = 'Chơi game',
                  rect = {x: 200, y: 400, width: 120, height: 40}, afterScroll = null,
                  card = {x: 180, y: 360, width: 240, height: 150}, cardAfterScroll = null} = {}) {
  const box = (r) => ({...r, bottom: r.y + r.height, right: r.x + r.width});
  const button = {
    disabled, className: buttonClass,
    innerText: buttonText,
    getAttribute: (key) => (key === 'title' ? '' : null),
    scrollIntoView: () => { if (afterScroll) Object.assign(button.current, afterScroll); },
    getBoundingClientRect: () => box(button.current),
    current: {...rect},
  };
  const node = {
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
    scrollIntoView: () => { if (cardAfterScroll) Object.assign(node.current, cardAfterScroll); },
    getBoundingClientRect: () => box(node.current),
    contains: (element) => !!element && !element.__outside,
    querySelector: (selector) => {
      if (selector === 'img') return alt === null ? null : {alt};
      if (selector === 'button.btn-main') return /\bbtn-main\b/.test(buttonClass) ? button : null;
      return selector === 'button' ? button : null;
    },
    current: {...card},
  };
  return {attrs, node};
}

function portalScan(items, {inIframe = false, innerHeight = 800, innerWidth = 600, hit = 'card'} = {}) {
  // elementFromPoint mô phỏng đúng ô thật nằm dưới điểm bấm: ngôi sao yêu thích che góc phải thẻ.
  const hits = {card: {closest: () => null}, fav: {closest: () => ({})},
                outside: {closest: () => null, __outside: true}, none: null};
  const ctx = {innerHeight, innerWidth,
    document: {querySelectorAll: (selector) => {
      const wanted = selector.split(',').map((part) => part.trim());
      assert.equal(wanted.length, 3);
      return items.filter((item) => wanted.some((part) => {
        const rule = /^\.game-item\[data-([a-z-]+)="([^"]+)"\]/.exec(part);
        return rule && item.attrs['data-' + rule[1]] === rule[2];
      })).map((item) => item.node);
    }, elementFromPoint: () => hits[hit]}};
  const win = {};
  win.top = inIframe ? {frameElement: true} : win;
  ctx.window = win;
  vm.createContext(ctx);
  return JSON.parse(JSON.stringify(vm.runInContext(entrySource, ctx)));
}

test('takes the pointer position of the Evolution play button on the entry page', () => {
  const box = portalScan([evoItem({attrs: {'data-subprovider': 'SA'}}), evoItem()]);
  assert.equal(box.x, 260);
  assert.equal(box.y, 420);
  assert.equal(box.label, 'Chơi game');
  assert.equal(box.via, 'button');
});

test('finds the current entry markup: data-gp evo tile with a plain "Bắt đầu" button', () => {
  const box = portalScan([evoItem({attrs: {'data-id': 'LIVE-EVO', 'data-gp': 'evo'}, alt: null,
                                    buttonClass: '', buttonText: 'Bắt đầu'})]);
  assert.equal(box.x, 260);
  assert.equal(box.y, 420);
  assert.equal(box.label, 'Bắt đầu');
  assert.equal(box.via, 'button');
});

test('clicks the tile itself when the start button stays display:none', () => {
  // Markup thật 11/2026: thẻ 239x154 có onclick, nút "Bắt đầu" ẩn (rect 0x0) dù hover.
  const box = portalScan([evoItem({attrs: {'data-id': 'LIVE-EVO', 'data-gp': 'evo'}, alt: null,
                                    buttonClass: '', buttonText: 'Bắt đầu',
                                    rect: {x: 0, y: 0, width: 0, height: 0}})]);
  assert.equal(box.via, 'card');
  assert.equal(box.x, 276);   // 40% chiều rộng thẻ, tránh ngôi sao yêu thích góc phải
  assert.equal(box.y, 453);   // 62% chiều cao thẻ
  assert.equal(box.label, 'LIVE-EVO');
});

test('scrolls a below-the-fold play button into view before measuring it', () => {
  const box = portalScan([evoItem({rect: {x: 200, y: 900, width: 120, height: 40},
                                    afterScroll: {x: 200, y: 400, width: 120, height: 40}})]);
  assert.equal(box.y, 420);
  assert.equal(box.via, 'button');
});

test('scrolls a below-the-fold tile into view before clicking it', () => {
  const box = portalScan([evoItem({rect: {x: 0, y: 0, width: 0, height: 0},
                                   card: {x: 180, y: 900, width: 240, height: 150},
                                   cardAfterScroll: {x: 180, y: 360, width: 240, height: 150}})]);
  assert.equal(box.via, 'card');
  assert.equal(box.y, 453);
});

test('clicks the visible band when the window clips the tile', () => {
  // Cửa sổ thấp hơn thẻ (lỗi thật 11/2026): điểm bấm phải nằm trong phần còn nhìn thấy.
  const box = portalScan([evoItem({rect: {x: 0, y: 0, width: 0, height: 0},
                                   card: {x: 180, y: 360, width: 240, height: 150}})],
                         {innerHeight: 420, innerWidth: 600});
  assert.equal(box.via, 'card');
  assert.equal(box.x, 276);
  assert.equal(box.y, 397);   // 62% của dải 360..420 còn thấy, không phải 453 ngoài màn hình
});

test('refuses to click an iframe, a missing tile or a disabled button', () => {
  assert.deepEqual(portalScan([evoItem()], {inIframe: true}), {reason: 'not_top_frame'});
  assert.deepEqual(portalScan([evoItem({attrs: {'data-subprovider': 'SA'}})]), {reason: 'evo_item_not_found'});
  assert.deepEqual(portalScan([evoItem({disabled: true})]), {reason: 'evo_button_not_found'});
  assert.deepEqual(portalScan([evoItem({rect: {x: 0, y: 0, width: 0, height: 0},
                                          card: {x: 180, y: 900, width: 240, height: 150}})]),
                   {reason: 'evo_button_pending'});
});

test('refuses the tile point when the favourite star or nothing else is under it', () => {
  const hiddenButton = [evoItem({attrs: {'data-id': 'LIVE-EVO', 'data-gp': 'evo'}, alt: null,
                                 buttonClass: '', rect: {x: 0, y: 0, width: 0, height: 0}})];
  for (const hit of ('fav outside none'.split(' '))) {
    assert.deepEqual(portalScan(hiddenButton, {hit}), {reason: 'evo_card_blocked'});
  }
});

test('reports a pending button when the tile is found but nothing is measurable yet', () => {
  assert.deepEqual(portalScan([evoItem({attrs: {'data-gp': 'evo'}, alt: null,
                                        buttonClass: '', buttonText: 'Bắt đầu',
                                        rect: {x: 0, y: 0, width: 0, height: 0},
                                        card: {x: 180, y: 360, width: 4, height: 150}})]),
                   {reason: 'evo_button_pending'});
});

test('prefers the Evolution tile and stops when two tiles look identical', () => {
  const picked = portalScan([evoItem({alt: 'Ezugi', rect: {x: 0, y: 100, width: 120, height: 40}}),
                             evoItem({rect: {x: 200, y: 100, width: 120, height: 40}})]);
  assert.equal(picked.x, 260);
  const ambiguous = portalScan([evoItem(), evoItem()]);
  assert.equal(ambiguous.reason, 'evo_button_ambiguous');
  assert.equal(ambiguous.count, 2);
});
