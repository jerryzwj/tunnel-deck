"""TunnelDeck Windows 原生版：托盘常驻 + 内嵌窗口，不依赖 Docker。

- 自带 cloudflared.exe（打包进 exe），面板直接跑在本机
- 点窗口右上角 X 只是收进托盘，不退出、不断隧道
- 支持开机自启（托盘菜单可开关，写 Startup 目录的 .cmd）
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

APP_NAME = "TunnelDeck"
APP_VERSION = "1.0.8"
DEFAULT_PORT = 8787

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPDATA = os.environ.get("APPDATA") or os.path.expanduser("~")
CFG_DIR = os.path.join(APPDATA, APP_NAME)
CFG_PATH = os.path.join(CFG_DIR, "config.json")
DATA_DIR = os.path.join(CFG_DIR, "data")
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                           "Start Menu", "Programs", "Startup")
SHOW_FLAG = os.path.join(CFG_DIR, "show.flag")
AUTOSTART_CMD = os.path.join(STARTUP_DIR, "TunnelDeck.cmd")

_window = None
_icon = None
_quitting = False


# ---------------------------------------------------------------- helpers
def load_config():
    os.makedirs(CFG_DIR, exist_ok=True)
    cfg = {"port": DEFAULT_PORT, "bind": "127.0.0.1", "data_dir": ""}
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
        except (OSError, ValueError):
            pass
    else:
        write_config(cfg)
    return cfg


def write_config(cfg):
    os.makedirs(CFG_DIR, exist_ok=True)
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def data_dir():
    """数据目录：默认放在 %APPDATA%\\TunnelDeck\\data；config.json 里可以指向已有数据。"""
    d = load_config().get("data_dir") or DATA_DIR
    os.makedirs(d, exist_ok=True)
    return d


def resource_path(*parts):
    base = getattr(sys, "_MEIPASS", ROOT)
    return os.path.join(base, *parts)


def cloudflared_bin():
    for p in (resource_path("cloudflared.exe"), resource_path("bin", "cloudflared.exe"),
              os.path.join(ROOT, "windows", "cloudflared.exe")):
        if os.path.exists(p):
            return p
    return "cloudflared"


def msgbox(text, title=APP_NAME, style=0x40):
    ctypes.windll.user32.MessageBoxW(None, text, title, style)


def log(msg):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(os.path.join(CFG_DIR, "launcher.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg).rstrip() + "\n")
    except OSError:
        pass


def reveal_existing():
    """已经有一个实例在托盘里时，用 flag 文件通知它把窗口叫出来（跨进程比 Win32 可靠）。"""
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(SHOW_FLAG, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
        return True
    except OSError:
        return False


def watch_show_flag():
    while not _quitting:
        try:
            if os.path.exists(SHOW_FLAG):
                os.remove(SHOW_FLAG)
                show_window()
        except OSError:
            pass
        time.sleep(1)


def single_instance():
    kernel = ctypes.windll.kernel32
    handle = kernel.CreateMutexW(None, False, "Global\\TunnelDeckTray")
    if kernel.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return False, handle
    return True, handle


def base_url():
    cfg = load_config()
    return f"http://127.0.0.1:{int(cfg['port'])}"


def wait_healthy(url, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


# ---------------------------------------------------------------- autostart
def exe_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def autostart_on():
    return os.path.exists(AUTOSTART_CMD)


def set_autostart(enable):
    os.makedirs(STARTUP_DIR, exist_ok=True)
    if enable:
        content = "@echo off\r\nstart \"\" " + exe_command() + " --silent\r\n"
        with open(AUTOSTART_CMD, "w", encoding="gbk", newline="") as f:
            f.write(content)
    elif os.path.exists(AUTOSTART_CMD):
        os.remove(AUTOSTART_CMD)


# ---------------------------------------------------------------- server
class _LogSink:
    """windowed exe 没有 stdout/stderr，uvicorn 的日志配置会因为 isatty 崩掉。"""

    def write(self, msg):
        text = str(msg).rstrip()
        if text:
            log(text)

    def flush(self):
        pass

    def isatty(self):
        return False


def start_server():
    if sys.stdout is None:
        sys.stdout = _LogSink()
    if sys.stderr is None:
        sys.stderr = _LogSink()
    os.environ.setdefault("TD_NATIVE", "1")
    os.environ.setdefault("DATA_DIR", data_dir())
    os.environ.setdefault("CLOUDFLARED", cloudflared_bin())
    os.environ.setdefault("METRICS_BASE_PORT", "20241")
    os.environ.setdefault("TZ", "Asia/Shanghai")
    if getattr(sys, "frozen", False):
        # 让 app 包从 _MEIPASS 里 import
        sys.path.insert(0, getattr(sys, "_MEIPASS", ROOT))
    else:
        sys.path.insert(0, ROOT)

    import uvicorn
    from app.main import app

    cfg = load_config()
    uvicorn.run(app, host=cfg.get("bind", "127.0.0.1"), port=int(cfg["port"]),
                log_level="warning", access_log=False, log_config=None)


def start_server_thread():
    def run():
        try:
            log("server thread start")
            start_server()
        except Exception as e:  # 端口被占、依赖缺失等
            import traceback
            log("server failed:\n" + traceback.format_exc())
            msgbox(f"面板服务启动失败：{e}\n\n检查端口 {load_config()['port']} 是否被占用，"
                   f"或修改 {CFG_PATH} 里的 port 后重试。", style=0x10)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------- icon
def make_icon():
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 61, 61], radius=14, fill=(18, 26, 34, 255),
                        outline=(58, 168, 255, 255), width=2)
    d.rectangle([14, 20, 20, 44], fill=(58, 168, 255, 255))
    d.rectangle([26, 20, 32, 44], fill=(120, 200, 255, 255))
    d.polygon([(40, 20), (50, 20), (45, 34)], fill=(58, 168, 255, 255))
    d.rectangle([40, 37, 50, 44], fill=(58, 168, 255, 255))
    return img


# ---------------------------------------------------------------- window
def create_window(show=True):
    import webview

    window = webview.create_window(
        APP_NAME,
        base_url(),
        width=1120,
        height=780,
        min_size=(900, 620),
        background_color="#0b1018",
        hidden=not show,
        easy_drag=False,
    )

    def on_closing():
        """点 X 不退出：收进托盘，隧道照常跑。"""
        if _quitting:
            return True
        try:
            window.hide()
        except Exception:
            pass
        return False

    window.events.closing += on_closing

    if not show:
        # pywebview 在 Windows 上会把首个窗口显示出来，静默启动时立刻收回去
        def keep_hidden(*_args):
            try:
                window.hide()
            except Exception:
                pass

        window.events.loaded += keep_hidden
        window.events.shown += keep_hidden
    return window


def show_window():
    if _window is None:
        return
    try:
        _window.show()
        _window.restore()
        _window.on_top = True
        _window.on_top = False
    except Exception:
        pass


def quit_app():
    global _quitting
    _quitting = True
    try:
        from app import runner
        runner.stop_all()
    except Exception as exc:
        log(f"stop_all 失败: {exc}")
    try:
        if _window is not None:
            _window.destroy()
    except Exception:
        pass
    try:
        if _icon is not None:
            _icon.stop()
    except Exception:
        pass
    os._exit(0)


def kill_orphans():
    """上一份程序被强杀后 cloudflared 会留下来占着 metrics 端口，启动前先清掉自己的孤儿。"""
    if os.name != "nt":
        return
    try:
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | "
            "Where-Object { $_.CommandLine -like '*TunnelDeck*' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
            "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
            "Where-Object { $_.LocalPort -ge 20241 -and $_.LocalPort -le 20260 } | "
            "Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { "
            "$p = Get-Process -Id $_ -ErrorAction SilentlyContinue; "
            "if ($p -and $p.ProcessName -like 'cloudflared*') { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } }"
        )
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, timeout=25,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log("已清理残留 cloudflared 进程")
    except Exception as exc:
        log(f"清理残留进程失败: {exc}")


def start_tray():
    import pystray
    from pystray import Menu, MenuItem

    def open_panel(icon=None, item=None):
        show_window()

    def toggle_autostart(icon, item):
        set_autostart(not autostart_on())
        icon.update_menu()

    def open_data(icon=None, item=None):
        os.startfile(data_dir())

    def open_browser(icon=None, item=None):
        os.startfile(base_url())

    def do_quit(icon=None, item=None):
        quit_app()

    menu = Menu(
        MenuItem("打开面板", open_panel, default=True),
        MenuItem("在浏览器打开", open_browser),
        MenuItem("开机自启", toggle_autostart, checked=lambda i: autostart_on()),
        MenuItem("数据目录", open_data),
        Menu.SEPARATOR,
        MenuItem("退出", do_quit),
    )
    icon = pystray.Icon("tunneldeck", make_icon(), APP_NAME, menu)
    icon.run_detached()
    return icon


def main():
    global _window, _icon
    if "--autostart-on" in sys.argv:
        set_autostart(True)
        return
    if "--autostart-off" in sys.argv:
        set_autostart(False)
        return

    ok, handle = single_instance()
    if not ok:
        if not reveal_existing():
            try:
                os.startfile(base_url())
            except Exception:
                msgbox("TunnelDeck 已经在运行（托盘里找 TD 图标）。", style=0x40)
        return

    silent = "--silent" in sys.argv
    load_config()
    kill_orphans()
    start_server_thread()

    url = base_url()
    if not wait_healthy(url, timeout=40):
        msgbox(f"面板没能在 {url} 启动。\n\n打开 {CFG_PATH} 改一个没被占用的端口试试。", style=0x10)
        return

    try:
        _window = create_window(show=not silent)
        _icon = start_tray()
        threading.Thread(target=watch_show_flag, daemon=True).start()

        def boot(silent_mode):
            # GUI 起来后再决定是否压进托盘（pywebview 会先亮一下首个窗口）
            time.sleep(2.5)
            if silent_mode and _window is not None:
                try:
                    _window.hide()
                except Exception:
                    pass

        import webview
        webview.start(func=boot, args=(silent,))
    except Exception as e:
        # WebView2 缺失时退化成「只托盘 + 浏览器打开」
        msgbox(f"窗口组件不可用（{e}）。\n\n面板仍在运行：{url}\n点确定后用浏览器打开。", style=0x30)
        try:
            os.startfile(url)
        except Exception:
            pass
        if _icon is None:
            _icon = start_tray()
        while True:
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log("fatal:\n" + traceback.format_exc())
        msgbox("TunnelDeck 启动失败，详情见 %APPDATA%\\TunnelDeck\\launcher.log", style=0x10)
