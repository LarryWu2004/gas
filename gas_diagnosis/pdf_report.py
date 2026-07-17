"""Render the existing HTML diagnosis report to PDF with Chromium."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


_PRINT_STYLE = """
<style id="pdf-print-layout">
@page { size: A3 landscape; margin: 10mm; }
@media print {
  html, body {
    width: auto !important;
    min-width: 0 !important;
    background: #fff !important;
    -webkit-print-color-adjust: exact !important;
    print-color-adjust: exact !important;
  }
  .main { padding: 0 !important; gap: 12px !important; }
  .page-header { margin: 0 !important; }
  .kpi-row {
    grid-template-columns: repeat(4, minmax(0, 1fr)) !important;
    gap: 10px !important;
  }
  .kpi-card {
    box-shadow: none !important;
    break-inside: avoid;
  }
  .kpi-card, .main-card, .rules-panel { padding: 14px !important; }
  .aux-grid {
    grid-template-columns: repeat(4, minmax(0, 1fr)) !important;
  }
  .detail-row {
    display: block !important;
  }
  .rules-panel {
    margin-top: 12px !important;
    break-before: page;
  }
  .metric-grid, .rule-list { gap: 7px !important; }
  .rule-item, tr {
    break-inside: avoid;
  }
  .metric-details {
    break-inside: auto !important;
  }
  .metric-details > summary, .metric-detail-chip, .metric-detail-body p {
    break-inside: avoid;
  }
  .metric-hint, .metric-expand { display: none !important; }
  .metric-detail-body {
    display: grid !important;
    padding: 12px 14px !important;
  }
  .metric-detail-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr)) !important;
    gap: 8px !important;
  }
  table {
    break-inside: auto !important;
  }
  .chart-panel {
    overflow: visible !important;
    break-inside: avoid;
  }
  .chart-panel svg, .chart-panel .pressure-svg {
    width: 100% !important;
    min-width: 0 !important;
    max-width: 100% !important;
    height: auto !important;
  }
  a { color: inherit !important; text-decoration: none !important; }
}
</style>
"""


def find_chromium() -> Path:
    """Locate a Chromium-compatible browser used for deterministic PDF output."""
    configured = os.environ.get("GAS_CHROMIUM_PATH", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return candidate.resolve()
        raise RuntimeError(f"GAS_CHROMIUM_PATH 指定的浏览器不存在：{candidate}")

    for command in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "msedge"):
        resolved = shutil.which(command)
        if resolved:
            return Path(resolved).resolve()

    windows_candidates = (
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "~")).expanduser()
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "~")).expanduser()
        / "Google/Chrome/Application/chrome.exe",
    )
    for candidate in windows_candidates:
        if candidate.is_file():
            return candidate.resolve()

    raise RuntimeError(
        "未找到 PDF 渲染浏览器。容器部署请使用项目 Dockerfile；"
        "非容器部署请安装 Chromium、Chrome 或 Edge，或配置 GAS_CHROMIUM_PATH。"
    )


def _pdf_html(source: str) -> str:
    if "</head>" not in source.lower():
        raise ValueError("HTML 报告缺少 head 结束标签")
    printable = re.sub(
        r'<details(?P<attrs>[^>]*\bclass=["\'][^"\']*\bmetric-details\b[^"\']*["\'][^>]*)>',
        _open_metric_details,
        source,
        flags=re.IGNORECASE,
    )
    return re.sub(r"</head>", _PRINT_STYLE + "\n</head>", printable, count=1, flags=re.IGNORECASE)


def _open_metric_details(match: re.Match) -> str:
    """Open core metric details only in the temporary HTML used for PDF output."""
    attrs = match.group("attrs")
    if re.search(r"(?:^|\s)open(?:\s|=|$)", attrs, flags=re.IGNORECASE):
        return match.group(0)
    return f"<details{attrs} open>"


def _browser_command(browser: Path, html_path: Path, pdf_path: Path, profile_dir: Path, headless: str) -> list[str]:
    return [
        str(browser),
        headless,
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-breakpad",
        "--disable-crash-reporter",
        "--disable-sync",
        "--hide-scrollbars",
        "--noerrdialogs",
        "--no-pdf-header-footer",
        "--print-to-pdf-no-header",
        f"--user-data-dir={profile_dir}",
        f"--print-to-pdf={pdf_path}",
        html_path.as_uri(),
    ]


def render_html_to_pdf(html_path: str | Path, pdf_path: str | Path) -> str:
    """Render an HTML report to PDF while preserving its visual hierarchy."""
    source_path = Path(html_path).resolve()
    target_path = Path(pdf_path).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"HTML 报告不存在：{source_path}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    browser = find_chromium()
    timeout = max(10, min(180, int(os.environ.get("GAS_PDF_TIMEOUT_SECONDS", "60"))))

    with tempfile.TemporaryDirectory(prefix="gas-pdf-") as temp_value:
        temp_dir = Path(temp_value)
        printable_html = temp_dir / "report.html"
        printable_html.write_text(_pdf_html(source_path.read_text(encoding="utf-8")), encoding="utf-8")
        profile_dir = temp_dir / "browser-profile"
        browser_env = os.environ.copy()
        browser_env.update(
            {
                "HOME": str(temp_dir),
                "XDG_CACHE_HOME": str(temp_dir / "cache"),
                "XDG_CONFIG_HOME": str(temp_dir / "config"),
            }
        )
        errors: list[str] = []
        for headless in ("--headless=new", "--headless"):
            target_path.unlink(missing_ok=True)
            completed = subprocess.run(
                _browser_command(browser, printable_html, target_path, profile_dir, headless),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=browser_env,
                timeout=timeout,
                check=False,
            )
            if completed.returncode == 0 and target_path.is_file() and target_path.stat().st_size > 1024:
                if target_path.read_bytes()[:4] == b"%PDF":
                    return str(target_path)
            errors.append((completed.stderr or completed.stdout or f"exit={completed.returncode}")[-1200:])

    target_path.unlink(missing_ok=True)
    detail = " | ".join(value.strip() for value in errors if value.strip())
    raise RuntimeError(f"PDF 报告生成失败：{detail or '浏览器未生成有效 PDF 文件'}")
