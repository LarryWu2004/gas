"""燃气调压器健康诊断系统 - 桌面应用版

一键启动，内嵌浏览器窗口，无需安装任何依赖或打开外部浏览器。
"""

from __future__ import annotations

import io
import json as _json
import os
import sys
import threading
import time
import socket
import urllib.request
import zipfile
from pathlib import Path


# ── 暴露给前端 JS 的 API ──────────────────────
class _DesktopApi:
    """通过 webview.expose 暴露给前端调用的桌面功能。"""

    def __init__(self, port: int) -> None:
        self._port = port

    def save_reports_zip(self, reports_json: str) -> dict:
        """前端调用：生成诊断报告 ZIP 并弹出原生保存对话框。"""
        import webview as _webview

        try:
            reports = _json.loads(reports_json)
            if not reports or not isinstance(reports, list):
                return {"ok": False, "error": "报告列表为空"}

            # 1. 请求后端生成 ZIP
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._port}/api/reports_zip",
                data=_json.dumps({"reports": reports}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                zip_data = resp.read()

            if not zip_data:
                return {"ok": False, "error": "生成的 ZIP 为空"}

            # 2. 弹出 Windows 原生保存对话框（pywebview 6.x: window 实例方法）
            window = _webview.active_window() if hasattr(_webview, "active_window") else _webview.windows[0]
            result = window.create_file_dialog(
                _webview.SAVE_DIALOG,
                save_filename="diagnosis_html_reports.zip",
            )
            if not result:
                return {"ok": False, "error": "用户取消了保存"}

            # pywebview 返回元组，取第一个元素
            file_path = result[0] if isinstance(result, (tuple, list)) else result
            save_path = Path(str(file_path))
            save_path.write_bytes(zip_data)
            return {"ok": True, "path": str(save_path)}

        except urllib.error.HTTPError as e:
            return {"ok": False, "error": f"服务器错误: {e.code}"}
        except urllib.error.URLError:
            return {"ok": False, "error": "无法连接后端服务"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def _find_free_port(start: int = 8765, max_attempts: int = 20) -> int:
    """找一个可用端口。"""
    for offset in range(max_attempts):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("无法找到可用端口")


def _setup_sys_path():
    """确保 gas_diagnosis 包在 sys.path 中（打包后需要手动处理）。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后：_MEIPASS 是解压的资源目录
        bundle = str(sys._MEIPASS)
        if bundle not in sys.path:
            sys.path.insert(0, bundle)
    else:
        # 开发环境：确保项目根目录在 sys.path
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)


def _set_environment():
    """设置环境变量供 web_app.py 使用。"""
    if getattr(sys, "frozen", False):
        bundle_dir = str(sys._MEIPASS)
        data_dir = str(Path(sys.executable).parent)
        os.environ["GAS_BUNDLE_DIR"] = bundle_dir
        os.environ["GAS_DATA_DIR"] = data_dir
        os.environ["GAS_IS_FROZEN"] = "1"
    else:
        project_root = str(Path(__file__).resolve().parents[1])
        os.environ["GAS_BUNDLE_DIR"] = project_root
        os.environ["GAS_DATA_DIR"] = project_root
        os.environ["GAS_IS_FROZEN"] = "0"


def _run_server(host: str, port: int):
    """后台启动 HTTP 服务器。"""
    from gas_diagnosis.web_app import serve

    # 捕获标准输出/错误，减少控制台噪音
    try:
        serve(host=host, port=port, quiet=True)
    except Exception as e:
        print(f"[错误] HTTP 服务启动失败: {e}", file=sys.stderr)
        sys.exit(1)


def _start_with_webview(host: str, port: int):
    """使用 pywebview 内嵌浏览器窗口启动。"""
    try:
        import webview
    except ImportError:
        print("[警告] pywebview 未安装，将打开系统默认浏览器。")
        print("如需内嵌窗口体验，请运行: pip install pywebview")
        _start_with_browser(host, port)
        return

    api = _DesktopApi(port)

    url = f"http://{host}:{port}"

    window = webview.create_window(
        title="燃气调压器健康诊断系统",
        url=url,
        width=1320,
        height=840,
        min_size=(1024, 680),
        text_select=True,
        easy_drag=False,
        js_api=api,
    )

    webview.start(debug=False, http_server=False)


def _start_with_browser(host: str, port: int):
    """打开系统默认浏览器访问。"""
    import webbrowser

    url = f"http://{host}:{port}"
    print(f"\n燃气调压器健康诊断系统已启动：{url}")
    print("按 Ctrl+C 退出。\n")

    webbrowser.open(url)

    # 保持进程运行
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在关闭...")


def main():
    """主入口。"""
    print("=" * 54)
    print("  燃气调压器健康诊断系统  v1.0")
    print("  Gas Regulator Health Diagnosis System")
    print("=" * 54)

    _setup_sys_path()
    _set_environment()

    host = "127.0.0.1"
    port = _find_free_port()

    # 启动 HTTP 服务器（守护线程）
    server_thread = threading.Thread(
        target=_run_server,
        args=(host, port),
        daemon=True,
        name="http-server",
    )
    server_thread.start()

    # 等待服务器就绪
    for _ in range(30):
        try:
            with socket.create_connection((host, port), timeout=0.5):
                break
        except (OSError, ConnectionRefusedError):
            time.sleep(0.2)
    else:
        print("[错误] HTTP 服务器启动超时", file=sys.stderr)
        sys.exit(1)

    # 启动 UI
    _start_with_webview(host, port)


if __name__ == "__main__":
    main()
