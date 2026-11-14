# BCR Desktop

Bot bàn **Baccarat** (cầu 3-3 / 3-2) và **LONG-LONG** (hai bát vàng/đỏ) cho Windows.
Giao diện **Electron**, lõi Python chạy headless. Không còn giao diện tkinter.

```
Electron (View)  ──IPC──▶  engine.py (Transport)  ──▶  Controller  ──▶  Adapter / Model
      ▲                                                                        │
      └──────────────── sự kiện "state" / "log" (JSON mỗi dòng) ◀──────────────┘
```

## Cấu trúc

```
BCR/
├─ ui/                       View — Electron
│  ├─ main.js                cửa sổ, IPC, spawn engine Python (mỗi trò 1 tiến trình)
│  ├─ preload.js             contextBridge (không nodeIntegration)
│  └─ renderer/              index.html · app.js · styles.css
├─ bcr/
│  ├─ config.py              hằng số thời gian/ngưỡng dùng chung
│  ├─ models/                Model — logic thuần, không I/O
│  │  ├─ engine.py           hằng số Banker/Player/Tie, tỉ lệ trả thưởng
│  │  ├─ method.py           luật cầu: strong_side, check_table, plan
│  │  ├─ money.py            MoneyBook: vốn, lãi/lỗ, chốt lời, chặn thua
│  │  ├─ chips.py            chia tiền → mệnh giá chip
│  │  ├─ live_sim.py         LiveSimulation: vòng đời một phiên bàn
│  │  └─ longlong.py         LongLongSimulation + thang 8 bậc
│  ├─ adapters/              Adapter — Chrome/Selenium/CDP
│  │  ├─ browser.py          mở/gắn Chrome, quét DOM, đọc số dư
│  │  ├─ lobby_stream.py     bắt WebSocket sảnh (lịch sử bàn), mở/rời bàn
│  │  ├─ table_bets.py       đọc chip, đặt chip vào ô cược
│  │  ├─ longlong_table.py   bàn LONG-LONG: ô cược, khay chip, hoàn tác
│  │  ├─ human_mouse.py · ghost_mouse.py · mouse_chromedriver.py   chuột người thật
│  │  └─ reader.js           script đọc DOM (nạp qua CDP)
│  ├─ controllers/           Controller — composition root, headless
│  │  ├─ baccarat.py         BaccaratController
│  │  ├─ longlong.py         LongLongController
│  │  └─ longlong_auto.py    HandTracker (nhận ván mới, chống chốt trùng)
│  ├─ service/engine.py      Transport — JSON-RPC stdin/stdout
│  └─ __main__.py            `python -m bcr --game baccarat`
├─ tests/                    unittest + kiểm thử JS của reader
├─ tools/                    backtest + probe site (không cần cho vận hành)
├─ install-toan-bo.bat       cài Python deps + Electron binary
├─ Start.bat                 mở ứng dụng (Electron)
└─ setup-electron.ps1        tải/giải nén Electron (tự phục hồi khi lỗi)
```

Quy ước tầng (được khoá bằng `tests/test_architecture.py`):

| Tầng | Được import | Không được import |
|---|---|---|
| models | `bcr.config`, `bcr.models` | tkinter, selenium, websocket, adapters, controllers, service |
| adapters | `bcr.config`, `bcr.models` | controllers, service |
| controllers | models, adapters, config | service |
| service | controllers, adapters, config | (không chứa nghiệp vụ) |

## Chạy

```
install-toan-bo.bat     # 1 lần: pip install + npm install + Electron binary
Start.bat               # mở ứng dụng
```

Yêu cầu: Python 3.11+ (đã có `Add to PATH`) và Node.js LTS.

Chạy engine bằng tay (không cần Electron):

```
python -m bcr --game baccarat      # hoặc longlong
{"id":1,"method":"state"}          # gõ JSON mỗi dòng, Enter
```

Đọc 1 file lệnh: `python -m bcr --game baccarat < lenh.jsonl`

## Giao thức engine

| Loại | Dạng |
|---|---|
| Yêu cầu | `{"id":1,"method":"tool.start","params":{}}` |
| Trả lời | `{"id":1,"ok":true,"result":{...}}` hoặc `{"id":1,"ok":false,"error":"..."}` |
| Sự kiện | `{"event":"state","data":{...}}`, `{"event":"log","data":{...}}` |

Mỗi trò một tiến trình, một profile Chrome và một file phiên:

| Trò | Cổng CDP | Profile | File phiên |
|---|---|---|---|
| Baccarat | 9222 | `chrome_profile/` | `bcr_state_baccarat.json` |
| LONG-LONG | 9223 | `chrome_profile_longlong/` | `bcr_state_longlong.json` |

Lệnh Baccarat: `chrome.open|attach|close|window`, `lobby.enter|sweep|back`,
`tables`, `table.open|select|leave`, `tool.start|stop`, `scan`, `chips.read`,
`config.get|set`, `session.new|reset|recover`.
Lệnh LONG-LONG: `chrome.open|close|status|window`, `tool.start|stop`, `config.set`
(nhận cả `start_url`, `chrome_window`), `ladder.fromCapital`, `hand.manual`,
`session.reset`, `tick`.

## Tab Thống kê (LONG-LONG)

Đánh giá hiệu quả thực tế của phiên, tính từ **sổ tay đã cược** (`LongLongSimulation.log`),
nên chỉ tính tay có tiền trên bàn:

| Số liệu | Ý nghĩa |
|---|---|
| Tỉ lệ thắng | `thắng / (thắng + thua)`, kèm số tay đã cược |
| Chuỗi thua dài nhất | số tay thua liên tiếp nhiều nhất (và chuỗi đang chạy ở cuối sổ) |
| Lãi / lỗ phiên | tổng `delta` theo đơn vị sảnh, kèm tay lỗ nặng nhất |
| Chart tròn | tỉ lệ thắng/thua |
| Chart cột | phân bố độ dài chuỗi thua (cột đậm = chuỗi dài nhất) |
| Chart đường | lãi/lỗ luỹ kế theo từng tay + dải 40 tay gần nhất (W/L) |

Số liệu do `models/longlong.py::summarize_history` tính và controller đẩy lên trong
`state.stats`, nên giao diện chỉ việc vẽ. Nhập tay bằng `hand.manual` cũng đi đúng luồng
chốt lệnh nên có tính vào thống kê.

## Luật lọc bàn Baccarat

`models/method.py::check_table` — phải đạt cả 5 điều kiện:

1. Số tay đã đi trong khoảng `hands_min`–`hands_max` (mặc định 10–50)
2. Không có vệt dài hơn `max_streak` (mặc định 3 tay; tay Hoà không tính)
3. Chưa từng có cầu 3-3 (hai vệt 3 tay liền nhau)
4. Chênh lệch B/P **≥ `min_lead`** (mặc định 3)
5. Chênh lệch B/P **≤ `max_lead`** (mặc định 5; `0`/trống = không giới hạn)

Bên mạnh = bên nhiều tay hơn trên toàn shoe; **B = P thì ưu tiên bên đỏ (Banker)**
(`tie_side = "B"`, đổi được sang `"P"` hoặc `"run"` = theo vệt).

Trước khi tính cầu, danh sách bàn còn phải qua cổng kỹ thuật: cầu WebSocket khớp
cầu DOM (`road_match`/`screen_stable ≥ 2`), dữ liệu ≤ 30s, không xung đột, đúng
`game_type = baccarat`, có mã bàn, và bỏ qua bàn Siêu Tốc Độ.

## Kiểm thử

```
python -m unittest discover -s tests -t . -q        # toàn bộ (không cần Chrome)
node tests/test_reader.cjs                          # script đọc DOM
```

## Ghi chú

- **Đóng Chrome bằng tay**: app dò cổng CDP (mỗi 2 giây, có cache 3 giây) nên phát hiện
  ngay, đổi nút thành **“Mở lại Chrome”**, tạm dừng phiên và xoá danh sách bàn cũ. Bấm
  lại là Chrome mở lại bình thường — không cần khởi động lại app.
- `TỰ ĐẶT CHIP THẬT` mặc định **bật** (giữ hành vi bản Tk cũ); tắt switch này nếu
  chỉ muốn xem kế hoạch — khi tắt, tay không được ghi sổ (chỉ tính tay web đã xác nhận).
- Baccarat cần Chrome có CDP: bấm **Mở Chrome** trong app rồi đăng nhập/vào sảnh;
  nếu Chrome đã mở sẵn, bấm **Gắn Chrome đang mở**.
- Vanguard/anti-cheat không liên quan: ứng dụng chỉ điều khiển Chrome qua CDP.
