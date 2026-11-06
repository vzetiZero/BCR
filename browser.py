# -*- coding: utf-8 -*-
"""Chrome riêng cho BCR: đăng nhập thủ công, chỉ đọc kết quả từ trang được phép.

Chrome tự quản lý việc lưu profile; phiên có thể hết hạn. Không kiểm tra thông tin
đăng nhập, không đặt cược thật, không nạp extension; CDP chỉ đọc Chrome có sẵn.
"""

import json
import os
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, "chrome_profile")
READER_JS = os.path.join(HERE, "extension", "reader.js")
DRIVER_PATH = os.path.join(HERE, "drivers", "chromedriver.exe")
LOBBY_PATH = "/frontend/evo/r3"
START_URL = "https://www.neo179.com/gamelobby/live"
PORTAL_HOST = urlsplit(START_URL).hostname
PORTAL_PATH = urlsplit(START_URL).path
WIN_W, WIN_H = 1200, 900
# Giá trị mở đầu cho cửa sổ thường; người dùng sửa trong Cài đặt → Cửa sổ Chrome và
# app lưu vào bcr_state.json nên các con số dưới đây chỉ là mức dự phòng.
# Tỉ lệ render là cờ khởi động nên chỉ đổi có hiệu lực ở lần mở Chrome kế tiếp.
WIN_SCALE = 0.8
WIN_FULLSCREEN = False
SCALE_MIN, SCALE_MAX = 50, 200
SIZE_MIN, SIZE_MAX = (800, 600), (7680, 4320)
# Sảnh EVO mở từ trang vào chỉ còn sống nhờ các nhịp tim của chính tab trang vào.
# Chrome giảm nhịp/đóng băng tab bị che hoặc bị ẩn nên nhịp tim tắt, sảnh đang tốt
# chuyển thành "User authentication failed (EV.7)". Ba cờ dưới giữ nhịp JS đầy đủ
# cho mọi tab, không đổi dữ liệu gửi đi.
KEEPALIVE_FLAGS = [
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-features=CalculateNativeWinOcclusion",
]
MAX_FRAME_DEPTH = 4
MAX_SCAN_FRAMES = 64
PAGE_LOAD_TIMEOUT = 20
SCRIPT_TIMEOUT = 8
COMMAND_TIMEOUT = 25
CDP_PORT = 9222
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"E:\Tools\Google Chrome\App\Chrome-bin\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%USERPROFILE%\Desktop\Google Chrome\App\Chrome-bin\chrome.exe"),
)


def is_lobby_host(host):
    """Sảnh Evolution đổi mirror liên tục nên chỉ cần một tên miền DNS công khai.

    Không nhận IP, localhost hay tên nội bộ. Đường dẫn /frontend/evo/r3 được kiểm
    tra riêng trong is_lobby_url; nhờ vậy mirror hiện tại vẫn dùng được mà không
    phải sửa danh sách đuôi cứng mỗi lần site đổi tên.
    """
    if not isinstance(host, str) or not 4 <= len(host) <= 253:
        return False
    if host != host.lower() or host.startswith(".") or host.endswith(".") or ".." in host:
        return False
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return False
    if re.fullmatch(r"[0-9.]+", host) or ":" in host:
        return False
    return re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", host) is not None


def is_lobby_url(url):
    """URL sảnh EVO thật: https, tên miền công khai, đúng đường dẫn lobby, không userinfo."""
    if not isinstance(url, str) or not url or "\\" in url:
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return False
    try:
        parsed = urlsplit(url)
        path = parsed.path
        return (parsed.scheme == "https" and is_lobby_host(parsed.hostname)
                and (path == LOBBY_PATH or path.startswith(LOBBY_PATH + "/"))
                and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443))
    except (ValueError, TypeError):
        return False


def is_evo_entry_url(url):
    """One-time Evolution handoff; it is never a readable lobby."""
    if not isinstance(url, str) or not url or "\\" in url:
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and is_lobby_host(parsed.hostname)
                and parsed.path == "/entry" and parsed.username is None
                and parsed.password is None and parsed.port in (None, 443))
    except (ValueError, TypeError):
        return False


def is_portal_url(url):
    """Trang vào cổng: chỉ dùng https://www.neo179.com/gamelobby/live."""
    if not isinstance(url, str) or "\\" in url:
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == PORTAL_HOST
                and parsed.path.startswith(PORTAL_PATH)
                and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443))
    except (ValueError, TypeError):
        return False


def valid_site_url(url):
    """Chỉ HTTPS đúng host, cổng mặc định, không userinfo hoặc URL cục bộ."""
    return is_lobby_url(url) or is_portal_url(url)


def _safe_url(url):
    """Chỉ giữ origin/path, không chuyển query, hash hay userinfo sang UI."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host:
            host = "[" + host + "]"
        port = parsed.port
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            host += ":%d" % port
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (ValueError, TypeError, AttributeError):
        return ""


def _clean_report(report):
    """Giữ định dạng reader, chỉ xuất metadata kết quả đã lọc URL."""
    if not isinstance(report, dict) or not isinstance(report.get("tables"), list):
        return None
    page = report.get("page") or {}
    diag = report.get("diag") or {}
    if not isinstance(page, dict) or not isinstance(diag, dict):
        return None
    table_id = page.get("table_id", "")
    if (not isinstance(table_id, str) or len(table_id) > 128
            or any(not (ch.isalnum() or ch in "_.:-") for ch in table_id)):
        table_id = ""
    frame_url = _safe_url(page.get("url", ""))
    return {
        "page": {"url": frame_url, "table_id": table_id,
                 "title": str(page.get("title") or "")[:80]},
        "tables": report["tables"],
        "diag": {"kind": diag.get("kind", ""), "scanned": diag.get("scanned", 0),
                 "roads": diag.get("roads", 0), "frame": frame_url,
                 "auth_required": bool(diag.get("auth_required"))},
    }


def chrome_path():
    """Chọn Chrome mới nhất trong các vị trí ứng viên."""
    found = [(chrome_version(cand), cand) for cand in CHROME_CANDIDATES
             if os.path.isfile(cand)]
    return max(found, key=lambda v: tuple(int(x or 0) for x in v[0].split(".")))[1] if found else ""


def chrome_version(path=None):
    """Đọc thư mục version cạnh chrome.exe, không chạy Chrome hay đọc profile."""
    path = path or chrome_path()
    if not path:
        return ""
    app_dir = os.path.dirname(path)
    try:
        found = [n for n in os.listdir(app_dir)
                 if all(part.isdigit() for part in n.split("."))
                 and os.path.isdir(os.path.join(app_dir, n))]
    except OSError:
        return ""
    return max(found, key=lambda n: tuple(int(x) for x in n.split("."))) if found else ""


def driver_version(path=None):
    path = path or DRIVER_PATH
    if not os.path.isfile(path):
        return ""
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True,
                             timeout=15).stdout.split()
        return out[1] if len(out) > 1 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def driver_for():
    """Driver cục bộ khớp bản; nếu không, để Selenium Manager chọn driver."""
    cv = chrome_version()
    dv = driver_version()
    if cv and dv and cv.split(".")[:3] == dv.split(".")[:3]:
        return "local", "driver %s khớp Chrome %s" % (dv, cv)
    return "auto", "Selenium Manager chọn driver tương thích Chrome %s" % (cv or "đã cài")


def valid_table_id(table_id):
    return (isinstance(table_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", table_id) is not None)


def validated_endpoint(page):
    """Endpoint CDP phải là ws vòng lặp, cổng 9222, không userinfo/fragment/ký tự điều khiển."""
    endpoint = page.get("webSocketDebuggerUrl", "") if isinstance(page, dict) else ""
    if (not isinstance(endpoint, str) or "\\" in endpoint
            or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in endpoint)):
        raise ValueError("invalid_cdp_endpoint")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "ws" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.port != CDP_PORT or parsed.username is not None
            or parsed.password is not None or parsed.fragment):
        raise ValueError("invalid_cdp_endpoint")
    return endpoint


def cdp_port_closed(exc):
    """Cổng 9222 đóng = Chrome mà người dùng nhìn thấy không phải Chrome do app mở
    (không có --remote-debugging-port). Phải nói rõ để người dùng không đi tìm “lỗi
    đăng nhập” trong khi app chưa cắm được vào cửa sổ nào."""
    reason = getattr(exc, "reason", None)
    return isinstance(exc, ConnectionRefusedError) or isinstance(reason, ConnectionRefusedError)


CDP_CLOSED_NOTE = (
    "Không có Chrome nào đang mở ở chế độ app dùng (CDP 9222). Cửa sổ Chrome bạn thấy "
    "không do app mở nên app không thao tác được trong đó. Hãy tắt cửa sổ Chrome đó đi "
    "(tránh hai phiên cùng tài khoản — nguyên nhân site báo “Bạn đã đăng xuất”), bấm "
    "“Mở Chrome” trong app rồi đăng nhập lại một lần.")


def chrome_launch_failure_note(exc):
    """Lỗi hay gặp nhất khi mở Chrome là profile chrome_profile đang bị cửa sổ Chrome
    khác giữ: Chrome vừa mở tự thoát, chromedriver chỉ báo SessionNotCreatedError chung
    chung. Phải nói thẳng cách mở khoá vì cùng tình trạng hai cửa sổ cũng là nguồn gây
    hộp “Bạn đã đăng xuất” của site."""
    text = str(exc).lower()
    if ("already in use" in text or "user data directory" in text
            or "chrome instance exited" in text or "devtoolsactiveport" in text):
        return ("Chrome của app không mở được vì profile “%s” đang bị một cửa sổ Chrome khác "
                "giữ. Hãy tắt hết cửa sổ Chrome rồi bấm “Mở Chrome” lại — mở hai cửa sổ cùng "
                "một tài khoản là nguyên nhân site đá phiên “Bạn đã đăng xuất”."
                % os.path.basename(PROFILE_DIR))
    return ("Không mở được Chrome (%s). Kiểm tra Chrome, driver, mạng hoặc profile đang được dùng; "
            "không mở cửa sổ thay thế và không đóng Chrome có sẵn." % type(exc).__name__)


def _window_number(value, default):
    """Chấp nhận số người dùng gõ kiểu Việt ("1280,5") mà không cần chuẩn hoá trước."""
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def normalize_window(size=None, scale=None, fullscreen=None):
    """Đổi ô nhập của người dùng thành cấu hình cửa sổ an toàn (chặn số 0/âm/cỡ điên rồ).

    Kích thước được kẹp trong SIZE_MIN..SIZE_MAX, tỉ lệ trong SCALE_MIN..SCALE_MAX phần trăm.
    """
    try:
        width, height = size[0], size[1]
    except (TypeError, IndexError):
        width = height = None
    percent = int(round(_window_number(scale, WIN_SCALE * 100)))
    return {
        "width": min(SIZE_MAX[0], max(SIZE_MIN[0], int(_window_number(width, WIN_W)))),
        "height": min(SIZE_MAX[1], max(SIZE_MIN[1], int(_window_number(height, WIN_H)))),
        "percent": min(SCALE_MAX, max(SCALE_MIN, percent)),
        "fullscreen": WIN_FULLSCREEN if fullscreen is None else bool(fullscreen),
    }


def build_flags(pos=None, cdp=False, window=None):
    window = normalize_window() if window is None else window
    flags = [
        "--user-data-dir=%s" % PROFILE_DIR,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
    ] + KEEPALIVE_FLAGS + [
        "--window-size=%d,%d" % (window["width"], window["height"]),
        "--force-device-scale-factor=%s" % (window["percent"] / 100.0),
    ]
    if window["fullscreen"]:
        flags.append("--start-fullscreen")
    if cdp:
        flags.append("--remote-debugging-port=%d" % CDP_PORT)
    if pos and not window["fullscreen"]:
        flags.append("--window-position=%d,%d" % pos)
    return flags


class BrowserSession:
    """Quản lý WebDriver riêng hoặc gắn CDP chỉ đọc khi được yêu cầu."""

    def __init__(self):
        self.driver = None
        self.cdp_attached = False
        self.mode = ""
        self.note = ""
        self.url = START_URL
        self.lock = threading.RLock()
        self._reader = None
        self._reader_mtime_ns = None
        self._selected_handle = None
        self._driver_info = "Chưa kết nối Chrome."
        self.last_frames = 0
        self.last_diag = {}
        self.last_error = ""
        self.connection_state = "closed"
        self._last_cdp_targets = []

    @property
    def connected(self):
        return self.driver is not None or self.cdp_attached

    @property
    def driver_info(self):
        """Thông tin đã cache; UI không cần RPC hoặc chạy subprocess."""
        return self._driver_info

    @staticmethod
    def _cdp_json(resource):
        with urlopen("http://127.0.0.1:%d/json/%s" % (CDP_PORT, resource),
                     timeout=SCRIPT_TIMEOUT) as response:
            return json.load(response)

    def _cdp_pages(self):
        self._last_cdp_targets = self._cdp_json("list")
        return [page for page in self._last_cdp_targets
                if isinstance(page, dict) and page.get("type") == "page"
                and valid_site_url(page.get("url"))]

    def _cdp_page(self):
        pages = self._cdp_pages()
        # Ưu tiên tab sảnh EVO; nếu sảnh nằm trong iframe của trang vào thì lấy trang vào.
        for page in pages:
            if is_lobby_url(page.get("url")):
                return page
        return pages[0] if pages else None

    def attach(self):
        """Gắn Chrome hiện có qua CDP; không mở, đóng hay điều hướng tab."""
        with self.lock:
            if self.driver is not None:
                self.note = "Đang dùng phiên WebDriver; chưa chuyển sang CDP."
                return False
            self.last_frames = 0
            self.last_diag = {}
            self.last_error = ""
            try:
                if self._cdp_page() is None:
                    if any(isinstance(item, dict) and item.get("type") == "page"
                           and is_evo_entry_url(item.get("url"))
                           for item in self._last_cdp_targets):
                        self.note = ("Evolution đang dừng ở /entry: token mở game đã bị từ chối hoặc hết hạn "
                                     "(EV.3/EV.7). App không tải lại hay sửa link này; đăng nhập lại ở trang gốc "
                                     "rồi mở EVO từ nút của site.")
                        self.last_diag = {"reason": "evo_entry_auth_error"}
                        self.connection_state = "waiting"
                        return False
                    self.note = ("Không có tab nào trong Chrome CDP 9222 mở %s hoặc sảnh EVO "
                                 "(https://<mirror>%s)." % (PORTAL_HOST, LOBBY_PATH))
                    self.last_diag = {"reason": "no_approved_tab"}
                    self.connection_state = "waiting"
                    return False
                info = "Chrome hiện có · CDP 9222"
                try:
                    version = self._cdp_json("version").get("Browser")
                    if isinstance(version, str) and version:
                        info = version[:80] + " · CDP 9222"
                except Exception:
                    pass  # Metadata không bắt buộc để gắn tab đã được xác nhận.
                self.mode = "cdp"
                self.cdp_attached = True
                self._driver_info = info
                self.note = ("Đã gắn Chrome hiện có · CDP 9222; chỉ đọc, giữ nguyên phiên đăng nhập. "
                             "Bạn tự vào sảnh Baccarat rồi bấm “Nhận tab sảnh”.")
                self.connection_state = "waiting"
                return True
            except Exception as exc:
                self.note = CDP_CLOSED_NOTE if cdp_port_closed(exc) else \
                    "Không gắn được Chrome CDP 9222 (%s)." % type(exc).__name__
                self.last_error = self.note
                self.connection_state = "error"
                return False

    def wait_for_cdp(self, seconds=15):
        """Chrome do driver mở tự tạo cổng 9222, thường muộn hơn lệnh launch vài giây.
        Biết sớm để báo đúng: cổng lên rồi thì quét được cả sảnh, chưa lên thì app chỉ
        đọc được cầu đang hiển thị trên màn hình."""
        deadline = time.monotonic() + max(0, seconds)
        while True:
            try:
                self._cdp_json("version")
                return True
            except Exception:
                if time.monotonic() >= deadline:
                    return False
            time.sleep(0.5)

    @staticmethod
    def _browser_command(ws, request_id, method, params):
        ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
        deadline = time.monotonic() + COMMAND_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("cdp_timeout")
            ws.settimeout(remaining)
            message = json.loads(ws.recv())
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError("cdp_browser_command_failed")
            return message.get("result", {})

    def resize_window(self, window=None, pos=None):
        """Đưa cửa sổ Chrome về cấu hình người dùng qua CDP: toàn màn hình hoặc kích thước tùy chọn.

        Tỉ lệ render là cờ khởi động nên phần này chỉ đổi hình dạng cửa sổ; note luôn nêu
        tỉ lệ đang lưu để người dùng biết cần mở lại Chrome khi vừa sửa tỉ lệ.
        """
        import websocket

        window = normalize_window() if window is None else window
        try:
            page = self._cdp_page()
        except Exception as exc:
            return False, "Không tìm được tab Chrome CDP 9222 để đổi cửa sổ (%s)." % type(exc).__name__
        target_id = page.get("id") if isinstance(page, dict) else None
        if not (isinstance(target_id, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,120}", target_id)):
            return False, "Không xác định được tab để tìm cửa sổ Chrome."
        try:
            ws = websocket.create_connection(validated_endpoint(self._cdp_json("version")),
                                             suppress_origin=True, timeout=SCRIPT_TIMEOUT,
                                             redirect_limit=0,
                                             http_no_proxy=["127.0.0.1", "localhost", "::1"])
        except Exception as exc:
            return False, "Không mở được kênh CDP của Chrome (%s)." % type(exc).__name__
        scale_note = "tỉ lệ %d%% (áp dụng khi mở lại Chrome)" % window["percent"]
        try:
            window_id = self._browser_command(ws, 1, "Browser.getWindowForTarget",
                                              {"targetId": target_id}).get("windowId")
            if window["fullscreen"]:
                self._browser_command(ws, 2, "Browser.setWindowBounds",
                                      {"windowId": window_id, "bounds": {"windowState": "normal"}})
                self._browser_command(ws, 3, "Browser.setWindowBounds",
                                      {"windowId": window_id,
                                       "bounds": {"windowState": "fullscreen"}})
                return True, "Đã đưa Chrome về toàn màn hình · %s." % scale_note
            self._browser_command(ws, 2, "Browser.setWindowBounds",
                                  {"windowId": window_id, "bounds": {"windowState": "normal"}})
            bounds = {"width": window["width"], "height": window["height"],
                      "windowState": "normal"}
            if pos:
                bounds.update(left=int(pos[0]), top=int(pos[1]))
            self._browser_command(ws, 3, "Browser.setWindowBounds",
                                  {"windowId": window_id, "bounds": bounds})
            return True, "Đã đưa Chrome về %d × %d · %s." % (
                window["width"], window["height"], scale_note)
        except Exception as exc:
            return False, "Không đổi được kích thước cửa sổ Chrome (%s)." % type(exc).__name__
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def start(self, url=None, pos=(1290, 30), cdp=False, window=None):
        with self.lock:
            if self.cdp_attached:
                if self.alive():
                    self.note = "Đang gắn Chrome hiện có · CDP 9222; không mở cửa sổ mới."
                    return True
                self.note = "CDP đang tạm dừng; mở lại tab đúng site hoặc ngắt kết nối trước khi mở Chrome mới."
                return False
            url = START_URL if url is None else url
            if not valid_site_url(url):
                self.note = ("URL không được phép: chỉ https://%s%s hoặc sảnh EVO "
                             "https://<mirror>%s; cổng 443, không userinfo, không hỗ trợ "
                             "file hay trang khác." % (PORTAL_HOST, PORTAL_PATH, LOBBY_PATH))
                self.last_error = self.note
                if self.driver is None:
                    self.connection_state = "error"
                return False
            if self.driver is not None:
                if self.alive():
                    self.note = "Chrome đã mở; hãy đăng nhập và chọn bàn thủ công."
                    return True
                self.stop()
            self.url = url
            self.last_frames = 0
            self.last_diag = {}
            self.last_error = ""
            try:
                chrome = chrome_path()
                if not chrome:
                    raise FileNotFoundError("chrome.exe")
                kind, why = driver_for()
                self._start_driver(chrome, url, pos, kind, cdp, window)
                self.mode = "driver"
                self.note = why + " · Đăng nhập thủ công; chỉ đọc kết quả, cược chỉ mô phỏng."
                self.connection_state = "waiting"
                return True
            except Exception as exc:
                # Không đưa lỗi WebDriver thô (có thể chứa URL/token) vào UI.
                self.note = chrome_launch_failure_note(exc)
                self.last_error = self.note
                self.connection_state = "error"
                self.mode = ""
                return False

    def _start_driver(self, chrome, url, pos, kind, cdp=False, window=None):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        opts = Options()
        opts.binary_location = chrome
        opts.page_load_strategy = "eager"
        for flag in build_flags(pos, cdp, window):
            opts.add_argument(flag)
        service = Service(DRIVER_PATH) if kind == "local" else Service()
        driver = None
        try:
            driver = webdriver.Chrome(service=service, options=opts)
            client_config = getattr(driver.command_executor, "_client_config", None)
            if client_config is not None:
                client_config.timeout = COMMAND_TIMEOUT
            driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
            driver.set_script_timeout(SCRIPT_TIMEOUT)
            driver.implicitly_wait(0)
            driver.get(url)  # Điều hướng duy nhất; các bước tiếp theo đều do người dùng.
            handle = driver.current_window_handle
            caps = driver.capabilities
            dv = (caps.get("chrome", {}) or {}).get("chromedriverVersion", "").split(" ")[0]
            info = "Chrome %s · chromedriver %s" % (caps.get("browserVersion", "?"), dv or "?")
        except Exception:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
            try:
                service.stop()  # Chỉ service vừa tạo, không tìm/kill tiến trình Chrome.
            except Exception:
                pass
            raise
        self.driver = driver
        self._selected_handle = handle
        self._driver_info = info

    def alive(self):
        with self.lock:
            if self.cdp_attached:
                try:
                    running = self._cdp_page() is not None
                    if not running:
                        self.last_frames = 0
                        self.last_diag = {"reason": "no_approved_tab"}
                        self.last_error = ""
                        self.connection_state = "waiting"
                    elif self.connection_state in ("closed", "error"):
                        self.last_diag = {}
                        self.last_error = ""
                        self.connection_state = "waiting"
                    return running
                except Exception as exc:
                    self.last_frames = 0
                    self.last_diag = {}
                    self.last_error = "Không liên lạc được Chrome CDP 9222 (%s)." % type(exc).__name__
                    self.connection_state = "error"
                    return False
            if self.driver is None:
                return False
            try:
                running = bool(self.driver.window_handles)
                if not running:
                    self.connection_state = "closed"
                return running
            except Exception as exc:
                self.last_error = "Không liên lạc được Chrome (%s)." % type(exc).__name__
                self.connection_state = "error"
                return False

    def stop(self):
        # Giữ cùng lock đến khi quit xong: scan/start không đua với shutdown.
        with self.lock:
            if self.cdp_attached:
                self.cdp_attached = False
                self.mode = ""
                self._selected_handle = None
                self._driver_info = "Chưa kết nối Chrome."
                self.last_frames = 0
                self.last_diag = {}
                self.last_error = ""
                self.connection_state = "closed"
                self.note = "Đã ngắt CDP; Chrome và phiên đăng nhập vẫn được giữ nguyên."
                return self.note
            driver, self.driver = self.driver, None
            self.mode = ""
            self._selected_handle = None
            self._driver_info = "Chưa kết nối Chrome."
            self.last_frames = 0
            self.last_diag = {}
            self.last_error = ""
            self.connection_state = "closed"
            if driver is None:
                self.note = "Không có phiên Chrome do ứng dụng điều khiển."
            else:
                try:
                    driver.quit()
                    self.note = "Đã đóng phiên Chrome do ứng dụng mở. " + self.session_note()
                except Exception as exc:
                    self.last_error = "Không xác nhận được Chrome đã đóng (%s)." % type(exc).__name__
                    self.note = self.last_error + " Hãy tự đóng cửa sổ nếu còn mở; không force-kill."
            return self.note

    def reader_src(self):
        with self.lock:
            mtime_ns = os.stat(READER_JS).st_mtime_ns
            if self._reader is None or self._reader_mtime_ns != mtime_ns:
                with open(READER_JS, "r", encoding="utf-8") as fh:
                    self._reader = fh.read()
                self._reader_mtime_ns = mtime_ns
            return self._reader

    def _reader_script(self):
        # Root tự xác nhận đúng sảnh EVO; iframe được đọc dưới tab gốc đã duyệt.
        # Không truyền hostname vào JS: mirror đổi liên tục, chỉ cố định đường dẫn.
        return """
            if (window === window.top) {
                const u = new URL(location.href);
                const p = u.pathname;
                if (u.protocol !== 'https:' ||
                    (p !== '%s' && !p.startsWith('%s/')) ||
                    (u.port && u.port !== '443') || u.username || u.password) return null;
            }
        """ % (LOBBY_PATH, LOBBY_PATH) + self.reader_src() + """
            const report = window.__BCR_SCAN();
            const u = new URL(location.href);
            const safe = /^(https?:)$/.test(u.protocol) ? u.origin + u.pathname : '';
            const fragment = new URLSearchParams(u.hash.slice(1).split('?').pop());
            const tid = u.searchParams.get('table_id') || fragment.get('table_id') || '';
            report.page.url = safe;
            report.page.table_id = /^[A-Za-z0-9_.:-]{1,128}$/.test(tid) ? tid : '';
            // Màn hình bàn đã mở chỉ có một cầu: mã bàn lấy từ URL, gắn vào từng dòng.
            if (report.page.table_id)
                for (const t of report.tables) if (!t.table_id) t.table_id = report.page.table_id;
            report.diag.frame = safe;
            report.diag.auth_required = /User authentication failed|your session may be expired|Error Code:\\s*EV\\.7|Bạn đã đăng xuất|You have been logged out/i.test(document.body ? document.body.innerText : '');
            return report;
        """

    def _select_root(self, driver):
        """Ưu tiên tab mới nhất đúng site, kể cả khi tab cũ đóng hoặc đổi URL."""
        from selenium.common.exceptions import NoSuchWindowException

        handles = driver.window_handles
        if not handles:
            self._selected_handle = None
            self.connection_state = "closed"
            return None
        try:
            original = driver.current_window_handle
        except NoSuchWindowException:
            original = None
        for handle in reversed(handles):
            try:
                driver.switch_to.window(handle)
                driver.switch_to.default_content()
                if valid_site_url(driver.current_url):
                    self._selected_handle = handle
                    return handle
            except NoSuchWindowException:
                continue
        self._selected_handle = None
        if original in handles:
            try:
                driver.switch_to.window(original)
            except NoSuchWindowException:
                pass
        self.connection_state = "waiting"
        return None

    def _goto(self, driver, path):
        """Gọi dưới scan lock, sau khi đã xác nhận root; không điều hướng URL."""
        from selenium.webdriver.common.by import By

        for idx in path:
            frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
            driver.switch_to.frame(frames[idx])

    def _scan_cdp(self):
        """Chỉ đọc các default context của tab được phép; không chuyển frame."""
        with self.lock:
            self.last_frames = 0
            self.last_diag = {}
            self.last_error = ""
            ws = None
            try:
                import websocket

                page = self._cdp_page()
                if page is None:
                    self.connection_state = "waiting"
                    self.last_diag = {"reason": "no_approved_tab"}
                    return []
                endpoint = validated_endpoint(page)
                ws = websocket.create_connection(
                    endpoint, suppress_origin=True, timeout=SCRIPT_TIMEOUT, redirect_limit=0,
                    http_no_proxy=["127.0.0.1", "localhost", "::1"],
                )
                contexts = []
                limited = False
                sequence = 0

                def command(method, params=None):
                    nonlocal sequence, limited
                    sequence += 1
                    ws.send(json.dumps({"id": sequence, "method": method, "params": params or {}}))
                    deadline = time.monotonic() + SCRIPT_TIMEOUT
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("cdp_timeout")
                        ws.settimeout(remaining)
                        message = json.loads(ws.recv())
                        event = message.get("method")
                        if event == "Runtime.executionContextCreated":
                            context = message.get("params", {}).get("context", {})
                            if (context.get("auxData") or {}).get("isDefault") is True:
                                context_id = context["id"]
                                if context_id not in contexts:
                                    if len(contexts) < MAX_SCAN_FRAMES:
                                        contexts.append(context_id)
                                    else:
                                        limited = True
                        elif event in ("Runtime.executionContextDestroyed", "Runtime.executionContextsCleared"):
                            # Không giữ report từ context cũ khi frame/tab đang đổi.
                            raise InterruptedError("context_changed")
                        if message.get("id") == sequence:
                            if "error" in message:
                                detail = str(message["error"].get("message", "")).lower()
                                if "context" in detail or "navigat" in detail or "target closed" in detail:
                                    raise InterruptedError("context_changed")
                                raise RuntimeError("cdp_command_failed")
                            return message.get("result", {})

                command("Runtime.enable")
                root = command("Target.getTargetInfo").get("targetInfo", {})
                if (root.get("targetId") != page["id"] or root.get("type") != "page"
                        or not valid_site_url(root.get("url"))):
                    self.connection_state = "waiting"
                    self.last_diag = {"reason": "root_left_site"}
                    return []
                root_url = root["url"]
                if not contexts:
                    self.connection_state = "waiting"
                    self.last_diag = {"reason": "no_default_context"}
                    return []
                expression = "(function(){\n%s\n})()" % self._reader_script()
                reports = []
                best_count = -1
                while self.last_frames < len(contexts):
                    context_id = contexts[self.last_frames]
                    self.last_frames += 1
                    result = command("Runtime.evaluate", {
                        "expression": expression, "contextId": context_id,
                        "returnByValue": True, "silent": True,
                        "timeout": SCRIPT_TIMEOUT * 1000,
                    })
                    if "exceptionDetails" in result:
                        self.last_error = "Không đọc được frame (JavaScript)."
                        continue
                    rep = _clean_report(result.get("result", {}).get("value"))
                    if rep is not None:
                        if rep["diag"]["auth_required"]:
                            self.last_error = "Site báo phiên không hợp lệ/hết hạn (EV.7). Đăng nhập ở trang gốc rồi mở lại sảnh Baccarat trong Chrome này."
                            self.last_diag = {"reason": "authentication_required"}
                            self.connection_state = "waiting"
                            return []
                        count = len(rep["tables"])
                        if count >= best_count:
                            self.last_diag = dict(rep["diag"])
                            best_count = count
                        if count:
                            reports.append(rep)
                # RPC cuối cũng nhận các sự kiện hủy context đang chờ trên socket.
                root = command("Target.getTargetInfo").get("targetInfo", {})
                if (root.get("targetId") != page["id"] or root.get("type") != "page"
                        or root.get("url") != root_url or not valid_site_url(root.get("url"))):
                    self.connection_state = "waiting"
                    self.last_diag = {"reason": "root_left_site"}
                    return []
                self.last_diag["frames_limited"] = limited or self.last_frames < len(contexts)
                self.connection_state = "reading" if reports else ("error" if self.last_error else "waiting")
                return reports
            except InterruptedError:
                self.last_error = "Frame đang thay đổi; tạm dừng đọc và thử lại ở lượt sau."
                self.last_diag = {"reason": "frame_context_changed"}
                self.connection_state = "waiting"
                return []
            except Exception as exc:
                self.last_error = "Không quét được Chrome CDP 9222 (%s)." % type(exc).__name__
                self.last_diag = {}
                self.connection_state = "error"
                return []
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass

    def scan(self):
        """Chỉ đọc tab được phép, tối đa 64 context; WebDriver sâu 4 iframe."""
        with self.lock:
            if self.cdp_attached:
                return self._scan_cdp()
            self.last_frames = 0
            self.last_diag = {}
            self.last_error = ""
            driver = self.driver
            if driver is None:
                self.connection_state = "closed"
                return []
            reports = []
            root = None
            limited = False
            best_count = -1
            try:
                from selenium.common.exceptions import (
                    InvalidSessionIdException, NoSuchFrameException,
                    NoSuchWindowException, StaleElementReferenceException,
                )
                from selenium.webdriver.common.by import By

                root = self._select_root(driver)
                if root is None:
                    self.last_diag = {"status": self.connection_state, "reason": "no_approved_tab"}
                    return []
                # Không bao giờ sửa URL/hash của sảnh EVO: phiên launch của site gắn với
                # đúng ngữ cảnh đã mở. Đổi hash có thể làm EVO tải lại /entry và trả 403/EV.7.
                # Root được kiểm tra lại ngay trong JS; frame khác origin chỉ được
                # tiếp cận qua cây iframe của root đã duyệt. Lọc URL trước cả RPC.
                script = self._reader_script()
                stack = [()]
                visited = 0
                while stack and visited < MAX_SCAN_FRAMES:
                    path = stack.pop()
                    visited += 1
                    driver.switch_to.window(root)
                    driver.switch_to.default_content()
                    if not valid_site_url(driver.current_url):
                        self.connection_state = "waiting"
                        self.last_diag = {"reason": "root_left_site"}
                        return []
                    try:
                        self._goto(driver, path)
                        # WebDriver current_url luôn là URL của top-level context,
                        # kể cả khi context hiện tại là iframe khác origin.
                        if not valid_site_url(driver.current_url):
                            self.connection_state = "waiting"
                            self.last_diag = {"reason": "root_left_site"}
                            return []
                        self.last_frames += 1
                        n = len(driver.find_elements(By.CSS_SELECTOR, "iframe, frame"))
                        # Queue cũng bị giới hạn; lỗi reader ở cha không bỏ mất con.
                        remaining = MAX_SCAN_FRAMES - len(stack)
                        if len(path) < MAX_FRAME_DEPTH:
                            count = min(n, remaining)
                            limited = limited or n > count
                            stack.extend(path + (i,) for i in reversed(range(count)))
                        elif n:
                            limited = True
                        rep = _clean_report(driver.execute_script(script))
                    except (InvalidSessionIdException, NoSuchWindowException):
                        raise
                    except (NoSuchFrameException, StaleElementReferenceException, IndexError):
                        continue  # DOM đang thay đổi; thử lại ở lượt scan kế tiếp.
                    except Exception as exc:
                        self.last_error = "Không đọc được frame (%s)." % type(exc).__name__
                        continue
                    if rep is not None:
                        if rep["diag"]["auth_required"]:
                            self.last_error = "Site báo phiên không hợp lệ/hết hạn (EV.7). Đăng nhập ở trang gốc rồi mở lại sảnh Baccarat trong Chrome này."
                            self.last_diag = {"reason": "authentication_required"}
                            self.connection_state = "waiting"
                            return []
                        count = len(rep["tables"])
                        if count >= best_count:
                            self.last_diag = dict(rep["diag"])
                            best_count = count
                        if count:
                            reports.append(rep)
                # Loại toàn bộ kết quả nếu root đã rời site trong lúc đang đọc.
                driver.switch_to.default_content()
                if not valid_site_url(driver.current_url):
                    self.connection_state = "waiting"
                    self.last_diag = {"reason": "root_left_site"}
                    return []
                self.last_diag["frames_limited"] = limited or bool(stack)
                self.connection_state = "reading" if reports else ("error" if self.last_error else "waiting")
                return reports
            except Exception as exc:
                self.last_error = "Không quét được Chrome (%s)." % type(exc).__name__
                self.last_diag = {}
                self.connection_state = "error"
                return []
            finally:
                if root is not None:
                    try:
                        driver.switch_to.default_content()
                    except Exception:
                        pass

    def session_note(self):
        """Không đọc cookie, thông tin tài khoản hay file trong profile."""
        return ("Chrome tự quản lý việc lưu phiên trong profile riêng. Phiên có thể hết hạn; "
                "việc lưu profile không chứng minh đã đăng nhập. Hãy đăng nhập thủ công "
                "và xác nhận tài khoản trực tiếp trong Chrome.")
