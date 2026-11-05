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
  const ctx = {window: {}, location: {href: 'https://ivi4sibf.kurxcbnpmjvc.link/frontend/evo/r3/?token=not-output#category=all_games',
    origin: 'https://ivi4sibf.kurxcbnpmjvc.link', pathname: '/frontend/evo/r3/'},
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

function scanTableScreen(beads, href = 'https://ivi4sibf.kurxcbnpmjvc.link/frontend/evo/r3/#category=all_games&table_id=suropcmnbqpabwko') {
  const ctx = {window: {}, location: {href, origin: 'https://ivi4sibf.kurxcbnpmjvc.link', pathname: '/frontend/evo/r3/'},
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
