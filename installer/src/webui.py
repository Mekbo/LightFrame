# -*- coding: utf-8 -*-
"""极简本地 Web UI 服务。

用标准库 http.server + 系统默认浏览器呈现安装向导 / 启动设置，
不依赖 tkinter 或任何第三方 GUI 库（精简 Python 运行时同样可用）。
"""
import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class WebUI:
    """启动一个仅绑定 127.0.0.1 的随机端口 HTTP 服务，并打开浏览器。

    api(action, payload) -> dict 由调用方提供；GET/POST /api/<action> 都会转给它。
    """

    def __init__(self, html_path, api, idle_timeout=900, port_file=None):
        self.html = Path(html_path).read_text(encoding="utf-8")
        self.api = api
        self.idle_timeout = idle_timeout
        self.port_file = Path(port_file) if port_file else None
        self.last = time.time()
        self.server = None
        self._stop = threading.Event()

    def _handler(self):
        ui = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, body: bytes, ctype: str, code: int = 200):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    pass

            def _json(self, obj):
                self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

            def do_GET(self):
                ui.last = time.time()
                p = self.path.split("?")[0]
                if p in ("/", "/index.html"):
                    self._send(ui.html.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if p.startswith("/api/"):
                    try:
                        self._json(ui.api(p[5:], {}) or {})
                    except Exception as e:
                        self._json({"ok": False, "error": str(e)})
                    return
                self._send(b"not found", "text/plain; charset=utf-8", 404)

            def do_POST(self):
                ui.last = time.time()
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception:
                    data = {}
                p = self.path.split("?")[0]
                action = p[5:] if p.startswith("/api/") else p
                try:
                    self._json(ui.api(action, data) or {})
                except Exception as e:
                    self._json({"ok": False, "error": str(e)})

        return H

    def run(self, open_browser=True):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        port = self.server.server_address[1]
        url = "http://127.0.0.1:%d/" % port
        if self.port_file:
            try:
                self.port_file.parent.mkdir(parents=True, exist_ok=True)
                self.port_file.write_text(str(port), encoding="utf-8")
            except Exception:
                pass
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        while not self._stop.wait(0.8):
            if time.time() - self.last > self.idle_timeout:
                break
        try:
            self.server.shutdown()
        except Exception:
            pass
        if self.port_file:
            try:
                self.port_file.unlink()
            except Exception:
                pass
        return url

    def quit(self):
        self._stop.set()


def probe_port(port_file) -> bool:
    """单实例检查：已有控制台在跑则返回 True。"""
    try:
        p = Path(port_file)
        if not p.exists():
            return False
        port = int(p.read_text(encoding="utf-8").strip())
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/api/status" % port,
                                   timeout=0.8) as r:
            return r.status == 200
    except Exception:
        return False
