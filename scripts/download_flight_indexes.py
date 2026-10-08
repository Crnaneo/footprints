"""Download complete MrAirspace quarterly indexes into the application's cache.

Run with the project virtualenv. Ctrl+C preserves partial downloads; rerun to
resume. HTTP proxy discovery is disabled for all requests, including redirects.
OS-level TUN routing must be disabled or bypassed separately in FlClash.
No flight records or GPS points are read or changed.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date
import json
from pathlib import Path
import re
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=False)
from backend.flight_schedules import (
    _download, _valid, cache_dir, cancel_download, release_assets,
)


def quarter(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(20\d{2})[ -]?[Qq]([1-4])", value)
    if not match:
        raise argparse.ArgumentTypeError("季度格式应为 2024Q1")
    return int(match[1]), int(match[2])


def amount(size: int) -> str:
    return f"{size / 1e9:.3f} GB ({size / 2**30:.3f} GiB)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", type=quarter, default=(2024, 1))
    parser.add_argument("--to", dest="end", type=quarter, default=(2026, 2))
    parser.add_argument("--list", action="store_true", help="只显示大小，不下载")
    parser.add_argument("--retries", type=int, default=5, help="每个文件失败后的重试次数")
    args = parser.parse_args()
    if args.start > args.end or args.retries < 0:
        parser.error("开始季度不能晚于结束季度，重试次数不能为负数")
    directory = cache_dir()
    # Reuse the app's official release catalog and its refresh/fallback policy.
    print("网络模式：直连（忽略系统和环境 HTTP / HTTPS / SOCKS 代理）。", flush=True)
    print("若 FlClash 启用 TUN，仍需在 FlClash 中关闭 TUN 或配置直连路由。", flush=True)
    asyncio.run(release_assets(date(args.end[0], args.end[1] * 3 - 1, 15), trust_env=False))
    catalog = json.loads((directory / "releases.json").read_text())
    chosen = {}
    for asset in catalog["assets"]:
        key = asset["year"], asset["quarter"]
        if args.start <= key <= args.end:
            if key not in chosen or "detailed" in asset["name"]:
                chosen[key] = asset
    expected = {(year, q) for year in range(args.start[0], args.end[0] + 1)
                for q in range(1, 5) if args.start <= (year, q) <= args.end}
    if missing := expected - chosen.keys():
        parser.error(f"发布目录缺少季度：{sorted(missing)}，没有启动下载")
    total = remaining = 0
    for _, asset in sorted(chosen.items()):
        path = directory / asset["name"]
        ready = _valid(path, asset)
        partial = path.with_suffix(".part")
        received = min(partial.stat().st_size, asset["size"]) if partial.exists() else 0
        total += asset["size"]
        remaining += 0 if ready else asset["size"] - received
        print(f"{asset['name']}: {amount(asset['size'])} · "
              f"{'已完整缓存' if ready else '待下载／续传'}", flush=True)
    print(f"总大小：{amount(total)}；预计剩余传输：{amount(remaining)}")
    print(f"保存位置：{directory}", flush=True)
    if args.list:
        return 0
    for _, asset in sorted(chosen.items()):
        path = directory / asset["name"]
        if _valid(path, asset):
            continue
        # Explicitly running this command authorizes resuming a cancelled file.
        path.with_suffix(".cancel").unlink(missing_ok=True)
        for attempt in range(args.retries + 1):
            stop = threading.Event()

            def progress():
                partial = path.with_suffix(".part")
                previous = partial.stat().st_size if partial.exists() else 0
                last = time.monotonic()
                while not stop.wait(1):
                    partial = path.with_suffix(".part")
                    count = partial.stat().st_size if partial.exists() else 0
                    now = time.monotonic()
                    speed = max(0, count - previous) / max(now - last, 0.001)
                    percent = min(100, count * 100 / asset['size'])
                    eta = f"{(asset['size'] - count) / speed / 60:.1f} 分钟" if speed else "等待数据"
                    phase = "校验中" if count == asset['size'] else f"剩余 {eta}"
                    print(f"{asset['name']}: {percent:6.2f}% · {count / 1e6:.1f} / "
                          f"{asset['size'] / 1e6:.1f} MB · {speed / 1e6:.2f} MB/s · {phase}", flush=True)
                    previous, last = count, now

            threading.Thread(target=progress, daemon=True).start()
            try:
                print(f"下载 {asset['name']}，第 {attempt + 1} 次尝试", flush=True)
                _download(asset, trust_env=False)
            except KeyboardInterrupt:
                cancel_download(asset)
                print("\n已终止，部分文件保留；重新运行同一命令即可续传。")
                return 130
            finally:
                stop.set()
            if _valid(path, asset):
                print(f"完成并校验：{asset['name']}", flush=True)
                break
            if path.with_suffix(".cancel").exists():
                print("下载已被终止，保留部分文件。")
                return 130
            if attempt == args.retries:
                print("下载未完成（网络失败或其它进程正在下载）；部分文件保留，重新运行可继续。")
                return 1
            try:
                time.sleep(min(30, 2 ** (attempt + 1)))
            except KeyboardInterrupt:
                cancel_download(asset)
                print("\n已终止，重新运行可续传。")
                return 130
    print("全部索引已下载完成。可直接在应用中查询，无需重启后端。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
