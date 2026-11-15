// Electron main: cửa sổ + quản lý engine Python riêng cho từng trò.
// Mỗi trò = một tiến trình python riêng (profile Chrome + cổng CDP + file cấu hình riêng),
// nên thao tác ở tab này không ảnh hưởng tab kia.
const { app, BrowserWindow, ipcMain, shell, dialog } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');

const PROJECT_ROOT = path.resolve(__dirname, '..');
const PYTHON_CANDIDATES = [
  process.env.BCR_PYTHON,
  'python',
  path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python', 'Python312', 'python.exe'),
  path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python', 'Python313', 'python.exe'),
].filter(Boolean);

let mainWindow = null;

class Engine {
  constructor(game) {
    this.game = game;
    this.child = null;
    this.buffer = '';
    this.nextId = 1;
    this.pending = new Map();
    this.ready = false;
    this.stopping = false;      // đang tắt app: không báo lỗi pipe nữa
    this.stderrTail = '';       // dòng stderr cuối, để giải thích khi engine thoát
    this.restartTimer = null;   // hẹn giờ tự khởi động lại engine đã thoát
    this.autoRestarts = 0;      // số lần tự khởi động lại liên tiếp (chống lặp vô hạn)
    this.exitExplained = false; // đã báo lý do thoát (markDead) -> đừng báo lại
  }

  send(payload) {
    const child = this.child;
    if (!child || child.killed) return false;
    // Engine đã thoát thì stdin là pipe gãy: kiểm tra trước để không phát sinh EPIPE.
    if (!child.stdin || child.stdin.destroyed || !child.stdin.writable) return false;
    try {
      child.stdin.write(JSON.stringify(payload) + '\n');
      return true;
    } catch (error) {
      this.onPipeError(error);
      return false;
    }
  }

  /** Pipe hỏng (EPIPE): engine đã thoát. Nuốt lỗi, đánh dấu chết và khởi động lại sau. */
  onPipeError(error) {
    const code = (error && error.code) || '';
    if (code === 'EPIPE' || code === 'ERR_STREAM_DESTROYED' || code === 'ERR_STREAM_WRITE_AFTER_END') {
      this.markDead(`mất kết nối engine (${code})`);
      return;
    }
    this.emitEvent('log', { level: 'error', text: `engine: ${String((error && error.message) || error)}` });
  }

  /** Dọn engine đang chết: reject lệnh đang chờ, lần gọi sau sẽ spawn lại. */
  markDead(reason) {
    const child = this.child;
    const wasStopping = this.stopping;
    this.child = null;
    this.ready = false;
    // Reject trước khi kill: kill() bắn 'exit' ngay và handler exit sẽ ghi đè lý do
    // bằng câu chung "engine đã thoát", làm mất nguyên nhân thật (EPIPE...).
    for (const [, entry] of this.pending) entry.reject(new Error(reason));
    this.pending.clear();
    this.exitExplained = true;      // 'exit' theo sau không cần báo lại lý do
    if (child) {
      try { if (!child.killed) child.kill(); } catch (error) { /* bỏ qua */ }
    }
    if (!wasStopping) {
      this.emitEvent('log', { level: 'error', text: `${reason} — sẽ tự khởi động lại.` });
    }
  }

  start() {
    if (this.child) return;
    const python = PYTHON_CANDIDATES.find((candidate) => {
      if (candidate === 'python') return true;
      return fs.existsSync(candidate);
    }) || 'python';
    this.stopping = false;
    this.stderrTail = '';
    this.buffer = '';
    const child = spawn(python, ['-m', 'bcr.service.engine', '--game', this.game], {
      cwd: PROJECT_ROOT,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' },
      windowsHide: true,
    });
    this.child = child;

    // Thiếu các handler 'error' dưới đây thì lỗi spawn (không có python) hoặc EPIPE khi
    // engine thoát sẽ thành "Uncaught Exception" của main process — Electron bật hộp
    // thoại đỏ "A JavaScript error occurred in the main process" và app đứng.
    child.on('error', (error) => {
      const reason = `không chạy được engine (${error.code || error.message})`;
      if (this.child === child) this.markDead(reason);
      else this.emitEvent('log', { level: 'error', text: reason });
    });
    if (child.stdin) child.stdin.on('error', (error) => this.onPipeError(error));

    if (child.stdout) {
      child.stdout.setEncoding('utf8');
      child.stdout.on('data', (chunk) => this.onData(chunk));
      child.stdout.on('error', (error) => this.onPipeError(error));
    }
    if (child.stderr) {
      child.stderr.setEncoding('utf8');
      child.stderr.on('data', (chunk) => {
        const text = String(chunk).trim();
        if (text) this.stderrTail = text.slice(-300);
        this.emitEvent('log', { level: 'error', text: `engine stderr: ${text}` });
      });
      child.stderr.on('error', () => { /* chỉ là log chẩn đoán */ });
    }

    child.on('exit', (code, signal) => {
      const stopping = this.stopping;
      const explained = this.exitExplained;
      this.exitExplained = false;
      if (this.child === child) {
        this.child = null;
        this.ready = false;
      }
      for (const [, entry] of this.pending) entry.reject(new Error('engine đã thoát'));
      this.pending.clear();
      if (stopping) return;
      if (!explained) {
        const tail = this.stderrTail ? ` · ${this.stderrTail}` : '';
        this.emitEvent('log', {
          level: 'error',
          text: `engine đã thoát (${code === null ? signal : code})${tail} — lệnh kế tiếp sẽ khởi động lại.`,
        });
      }
      this.scheduleRestart();
    });
  }

  /** Tự dựng lại engine vừa thoát để UI không "đứng" mà không biết vì sao. */
  scheduleRestart() {
    if (this.stopping || this.restartTimer || this.child) return;
    if (this.autoRestarts >= 5) {
      this.autoRestarts = 0;      // thao tác kế tiếp của người dùng sẽ thử lại
      this.emitEvent('log', {
        level: 'error',
        text: 'engine thoát 5 lần liên tiếp — đã ngừng tự khởi động lại. Bấm lại thao tác để thử.',
      });
      return;
    }
    const delay = Math.min(2000 * (2 ** this.autoRestarts), 20000);
    this.autoRestarts += 1;
    this.restartTimer = setTimeout(() => {
      this.restartTimer = null;
      if (this.stopping || this.child) return;
      this.start();
    }, delay);
  }

  onData(chunk) {
    this.buffer += chunk;
    let index = this.buffer.indexOf('\n');
    while (index >= 0) {
      const line = this.buffer.slice(0, index).trim();
      this.buffer = this.buffer.slice(index + 1);
      if (line) this.onLine(line);
      index = this.buffer.indexOf('\n');
    }
  }

  onLine(line) {
    let message = null;
    try {
      message = JSON.parse(line);
    } catch (error) {
      this.emitEvent('log', { level: 'error', text: `engine: ${line.slice(0, 200)}` });
      return;
    }
    if (message.event) {
      if (message.event === 'ready') {
        this.ready = true;
        this.autoRestarts = 0;
      }
      this.emitEvent(message.event, message.data);
      return;
    }
    const entry = this.pending.get(message.id);
    if (!entry) return;
    this.pending.delete(message.id);
    if (message.ok) entry.resolve(message.result);
    else entry.reject(new Error(message.error || 'engine báo lỗi'));
  }

  emitEvent(event, data) {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('engine:event', { game: this.game, event, data });
    }
  }

  call(method, params = {}, timeout = 30000) {
    this.start();
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`hết thời gian chờ ${method}`));
      }, timeout);
      this.pending.set(id, {
        resolve: (value) => { clearTimeout(timer); resolve(value); },
        reject: (error) => { clearTimeout(timer); reject(error); },
      });
      if (!this.send({ id, method, params })) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new Error('không gửi được lệnh tới engine'));
      }
    });
  }

  stop() {
    this.stopping = true;
    if (this.restartTimer) {
      clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }
    if (!this.child) return;
    this.send({ id: 0, method: 'shutdown', params: {} });
    const child = this.child;
    setTimeout(() => {
      try { if (!child.killed) child.kill(); } catch (error) { /* bỏ qua */ }
    }, 1500);
  }
}

const engines = {
  baccarat: new Engine('baccarat'),
  longlong: new Engine('longlong'),
};

// Lưới an toàn cuối: Electron biến mọi exception chưa bắt ở main process thành hộp
// thoại đỏ "A JavaScript error occurred in the main process" và app đứng. Ghi log rồi
// tiếp tục để người dùng thấy nguyên nhân trong tab Nhật ký thay vì bị chặn.
function reportMainError(kind, error) {
  const text = `${kind}: ${String((error && error.stack) || (error && error.message) || error).split('\n')[0]}`;
  console.error(text);
  for (const engine of Object.values(engines)) {
    engine.emitEvent('log', { level: 'error', text });
  }
}

process.on('uncaughtException', (error) => reportMainError('lỗi main process', error));
process.on('unhandledRejection', (reason) => reportMainError('promise bị từ chối', reason));

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 800,
    minWidth: 1000,
    minHeight: 680,
    frame: false,
    backgroundColor: '#0b1220',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('closed', () => { mainWindow = null; });
}

ipcMain.handle('engine:call', async (_event, { game, method, params }) => {
  const engine = engines[game];
  if (!engine) throw new Error(`trò không hỗ trợ: ${game}`);
  const started = Date.now();
  try {
    const result = await engine.call(method, params || {});
    return { ok: true, result, ms: Date.now() - started };
  } catch (error) {
    return { ok: false, error: String(error.message || error), ms: Date.now() - started };
  }
});

ipcMain.handle('app:info', () => ({
  version: app.getVersion(),
  root: PROJECT_ROOT,
  engines: Object.keys(engines),
}));

ipcMain.handle('app:openExternal', (_event, url) => shell.openExternal(url));

// Lưu file text (CSV lịch sử bàn) qua hộp thoại của Windows.
ipcMain.handle('app:saveText', async (_event, { name, text }) => {
  try {
    const result = await dialog.showSaveDialog(mainWindow, {
      title: 'Lưu file',
      defaultPath: path.join(PROJECT_ROOT, name || 'bcr-export.csv'),
      filters: [{ name: 'CSV', extensions: ['csv'] }, { name: 'Tất cả', extensions: ['*'] }],
    });
    if (result.canceled || !result.filePath) return { ok: false, error: 'Đã huỷ lưu file.' };
    await fs.promises.writeFile(result.filePath, String(text ?? ''), 'utf8');
    return { ok: true, path: result.filePath };
  } catch (error) {
    return { ok: false, error: `Không lưu được file: ${error.message || error}` };
  }
});

ipcMain.on('window:control', (_event, action) => {
  if (!mainWindow) return;
  if (action === 'minimize') mainWindow.minimize();
  else if (action === 'maximize') {
    if (mainWindow.isMaximized()) mainWindow.unmaximize();
    else mainWindow.maximize();
  } else if (action === 'close') mainWindow.close();
});

app.whenReady().then(() => {
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('before-quit', () => {
  Object.values(engines).forEach((engine) => engine.stop());
});

app.on('window-all-closed', () => {
  Object.values(engines).forEach((engine) => engine.stop());
  if (process.platform !== 'darwin') app.quit();
});
