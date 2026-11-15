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
(nhận cả `unit_mode`, `start_url`, `chrome_window`), `ladder.fromCapital`, `hand.manual`,
`session.reset`, `tick`.

## Đơn vị tiền trên sàn (LONG-LONG)

Một bậc thang cược luôn là "đv" = 1.000đ, nhưng **số tiền gửi cho sàn phải cùng thang
với khay chip**, nên tab Cấu hình có 2 lựa chọn (`config.unit_mode`):

| `unit_mode` | Khay chip của sàn | Bậc 10 đv gửi đi |
|---|---|---|
| `vn2` (mặc định) | in VN2 — chip 40 = 40.000đ | `10` (= 10.000đ) |
| `vnd` | in tiền thật — chip 10.000 = 10.000đ | `10000` (= 10.000đ) |

Quy đổi nằm ở `models/longlong.py`: `to_site_amount()` (đv → số gửi sàn) và
`from_site_amount()` (số đọc trên sàn → đv, dùng khi so số dư). Chọn sai thì tool báo
“Không chia được … theo mệnh giá” và **gợi ý đổi chế độ**, không tự đặt số lớn hơn.

## Chống “ván ma” và độ trễ khi đặt chip (LONG-LONG)

Sàn báo ô thắng bằng class hiệu ứng, và rê chuột lên ô cược cũng gắn class — nên bộ đếm
ván dễ sinh **ván không có thật** (sổ vốn cộng/trừ tiền cho tay chưa mở). Bốn lớp chặn:

| Lớp | Ở đâu | Tác dụng |
|---|---|---|
| Khối KẾT QUẢ của sàn phải đổi | `controllers/longlong_auto.py` | hover/animation không làm khối `[data-role="result"]` đổi → không sinh ván ma |
| Xác nhận `confirm_reads` lần đọc liên tiếp | idem | lọc nhiễu animation |
| Hai ván cách nhau ≥ `min_hand_gap` giây | idem | chặn 2 “ván” cách nhau 2 giây |
| Lệnh gắn số ván (`pending["hand"]`) | `controllers/longlong.py` | ván khác ra kết quả thì **bỏ lệnh**, không tính tiền |

Thêm hai lớp chống tính tiền oan:

- **Chip phải còn trên bàn**: sau khi bấm, tool đọc `total_bet` mỗi nhịp; nếu tiền biến
  mất quá `chip_watch_seconds` giây mà chưa có kết quả → sàn đã huỷ/hoàn tiền → bỏ ván,
  không ghi sổ.
- **Không bấm khi sàn báo đã đóng cược**: `[data-role="status-text"]` chứa “kết quả”,
  “no more bets”, “chờ kết quả”… thì `place()` từ chối (kèm câu trạng thái trong nhật ký).

Khi đang cần bấm chip mà chưa bấm được, vòng đọc tự nhanh lên 0.7 giây (`PLACE_POLL_SECONDS`)
để không trễ cửa sổ cược. `adapters/longlong_table.py::place()` đọc lại TỔNG ĐẶT CƯỢC
**sau từng chip**; cú bấm nào không vào bàn thì dừng và HOÀN TÁC ngay, và tự đưa chuột
ra khỏi ô cược (cả trước mỗi lần đọc) để hover không bị đọc thành “ô thắng”.

### Đọc thẳng luồng WebSocket của sàn (nguồn chuẩn)

Sàn đẩy trạng thái ván qua WebSocket của game (`/frontend/evo/...`). Đã bắt thật trên bàn
`DragonDragon0001` — message dạng:

```json
{"type":"twocoins.state","args":{
   "gameId":"18e81b2596ce956a1918740c","number":"03:45:03",
   "phase":{"type":"BetsOpen","history":[],"previousChips":{"JNS_C":80000},
            "timeRemaining":14986},
   "chips":{}}}
```

Chuỗi phase một ván (~39 giây): `BetsOpen` (15s) → `BetsClosedAnnounced` → `BetsClosed`
→ `BowlOneShook` → `BowlTwoShook` → `CoinOneReveal` → `CoinTwoReveal` → `CoinsRevealed`
→ `GameResolved` (`coins: ["Gold","Red"]`).

`adapters/longlong_stream.py` đọc luồng này và cho biết:

| Dùng để | Lấy từ |
|---|---|
| Được đặt chip hay chưa | `phase.type == "BetsOpen"` (+ còn > `min_bet_ms`) |
| Lệnh thuộc ván nào | `args.gameId` (gắn vào `pending["round"]`) |
| Kết quả ván | `GameResolved` → `coins` → `VV/DD/VD/DV` |
| Đối chiếu tiền đã vào bàn | `phase.previousChips` |

`Gold+Gold = VV` (2 vàng), `Red+Red = DD` (2 đỏ), `Gold+Red = VD`, `Red+Gold = DV`.

Nhờ vậy tool **không bấm khi cửa sổ cược đóng** (hết cảnh "ô chưa mở"), và **chỉ tất
toán kết quả của đúng ván mình đã đặt**. Nếu không đọc được luồng (Chrome đóng, sàn đổi
định dạng) thì tự lùi về cách đọc ô thắng như trước.

Nhiều sàn (ví dụ 9vnv và 1x) nhúng **cùng một vỏ game Evolution** nên định dạng message
giống nhau; tool tự chọn tab nào thực sự phát `twocoins.*` trong Chrome, nên mở nhiều
site cùng lúc vẫn đúng. Bắt tay frame của một sàn mới:

```
python tools/probe_longlong.py --port 9223 --ws 60
```

## Dừng theo lãi mục tiêu % (LONG-LONG)

Cấu hình → khu An toàn → **“Dừng tool khi lãi đạt (%)”** (0 = tắt).

- Vốn đầu phiên được **chốt tại lúc bấm Chạy tool**, lấy từ **số dư thật trên sàn**; nếu
  chưa vào bàn thì tạm dùng “Vốn phiên” và sẽ chốt lại bằng số dư thật ngay khi đọc được.
- Ngưỡng dừng = `vốn × (1 + %/100)`. Ví dụ vốn 3.000 đv + 30% → **3.900 đv** thì tool tự
  dừng và ghi nhật ký `Đạt lãi mục tiêu +30% … DỪNG TOOL`.
- Đạt ngưỡng giữa ván thì dừng ngay: nhịp đó không đặt thêm chip, vòng đọc kết thúc.
- Đổi % trong Cấu hình sẽ xoá trạng thái “đã dừng” để chạy tiếp được; “Reset phiên” xoá
  vốn đầu phiên và lần Chạy tool sau sẽ chốt lại.
- Thẻ **LÃI / LỖ PHIÊN** hiện thêm `mục tiêu +30%: 3.000 → 3.900 đv · đang +10.0%`.

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
