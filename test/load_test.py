#!/usr/bin/env python3
import argparse, asyncio, time, statistics
from collections import defaultdict
import httpx

def parse_headers(hlist):
    headers = {}
    for h in hlist or []:
        if ":" not in h:
            raise ValueError(f"Bad header format: {h!r}. Use 'Key: Value'.")
        k,v = h.split(":",1)
        headers[k.strip()] = v.strip()
    return headers

class Stats:
    def __init__(self):
        self.latencies = []          # seconds
        self.status_counts = defaultdict(int)
        self.error_counts = defaultdict(int)
        self.ok = 0
        self.total = 0

    def record(self, lat_s, status):
        self.total += 1
        self.status_counts[status] += 1
        if 200 <= status < 400:
            self.ok += 1
        self.latencies.append(lat_s)

    def record_error(self, name):
        self.total += 1
        self.error_counts[name] += 1

    def pct(self, p):
        if not self.latencies:
            return float("nan")
        s = sorted(self.latencies)
        k = (len(s)-1) * p
        f = int(k)
        c = min(f+1, len(s)-1)
        if f == c: return s[f]
        return s[f] + (s[c]-s[f]) * (k - f)

async def do_request(client, method, url, headers, body, stats, timeout):
    start = time.perf_counter()
    try:
        r = await client.request(method, url, headers=headers, content=body, timeout=timeout)
        stats.record(time.perf_counter() - start, r.status_code)
    except Exception as e:
        stats.record_error(type(e).__name__)

async def warmup(client, method, url, headers, body, seconds, concurrency, timeout):
    if seconds <= 0 or concurrency <= 0:
        return
    stop_at = time.perf_counter() + seconds
    sem = asyncio.Semaphore(concurrency)
    async def worker():
        while time.perf_counter() < stop_at:
            async with sem:
                await do_request(client, method, url, headers, body, Stats(), timeout)
    await asyncio.gather(*[worker() for _ in range(concurrency)])

async def run_closed_loop(client, method, url, headers, body, duration, concurrency, timeout, progress=False):
    stats = Stats()
    stop_at = time.perf_counter() + duration
    sem = asyncio.Semaphore(concurrency)

    async def ticker():
        last_t = time.perf_counter()
        last_n = 0
        while time.perf_counter() < stop_at:
            await asyncio.sleep(1.0)
            if progress:
                now, n = time.perf_counter(), stats.total
                print(f"[{int(now - (stop_at - duration))}s] total={n} ok={stats.ok} "
                      f"errs={sum(stats.error_counts.values())} inst_rps={n-last_n}/s")
                last_t, last_n = now, n

    async def worker():
        while time.perf_counter() < stop_at:
            async with sem:
                await do_request(client, method, url, headers, body, stats, timeout)

    await asyncio.gather(ticker(), *[worker() for _ in range(concurrency)])
    return stats

async def run_open_loop(client, method, url, headers, body, duration, rps, concurrency, timeout, progress=False):
    stats = Stats()
    sem = asyncio.Semaphore(concurrency)
    start = time.perf_counter()
    end = start + duration
    next_t = start
    tasks = []

    async def launch_one():
        async with sem:
            await do_request(client, method, url, headers, body, stats, timeout)

    # progress ticker
    async def ticker():
        last_n = 0
        while time.perf_counter() < end:
            await asyncio.sleep(1.0)
            if progress:
                n = stats.total
                print(f"[{int(time.perf_counter() - start)}s] sent={n} ok={stats.ok} "
                      f"errs={sum(stats.error_counts.values())} inst_rps={n-last_n}/s")
                last_n = n

    async def scheduler():
        nonlocal next_t
        period = 1.0 / rps if rps > 0 else 0
        while True:
            now = time.perf_counter()
            if now >= end:
                break
            if now < next_t:
                await asyncio.sleep(next_t - now)
                continue
            tasks.append(asyncio.create_task(launch_one()))
            next_t += period

    await asyncio.gather(scheduler(), ticker())
    if tasks:
        await asyncio.gather(*tasks)
    return stats

def summarize(stats, duration):
    total = stats.total
    ok = stats.ok
    errs = sum(stats.error_counts.values())
    statuses = ", ".join(f"{k}:{v}" for k,v in sorted(stats.status_counts.items()))
    achieved_rps = total / duration if duration > 0 else float("nan")
    if stats.latencies:
        p50 = stats.pct(0.50) * 1000
        p90 = stats.pct(0.90) * 1000
        p95 = stats.pct(0.95) * 1000
        p99 = stats.pct(0.99) * 1000
        avg = (sum(stats.latencies)/len(stats.latencies)) * 1000
        mn  = min(stats.latencies) * 1000
        mx  = max(stats.latencies) * 1000
    else:
        p50=p90=p95=p99=avg=mn=mx=float("nan")
    success_rate = (ok / total * 100.0) if total else 0.0

    print("\n=== Results ===")
    print(f"Total responses: {total}")
    print(f"2xx-3xx (success): {ok}  |  Errors (exceptions): {errs}")
    print(f"HTTP status breakdown: {statuses or 'n/a'}")
    print(f"Success rate: {success_rate:.2f}%")
    print(f"Achieved throughput: {achieved_rps:.2f} req/s")
    print(f"Latency ms: avg {avg:.1f}  p50 {p50:.1f}  p90 {p90:.1f}  p95 {p95:.1f}  p99 {p99:.1f}  min {mn:.1f}  max {mx:.1f}")

async def main():
    ap = argparse.ArgumentParser(description="Tiny async load tester (httpx + asyncio)")
    ap.add_argument("--url", required=True, help="Full URL, e.g. http://localhost:8080/compute?n=400000")
    ap.add_argument("--method", default="GET")
    ap.add_argument("--header", action="append", help="Header 'Key: Value' (repeatable)")
    ap.add_argument("--body", default=None, help="Request body for POST/PUT")
    ap.add_argument("--duration", type=float, default=30.0, help="Seconds of measured load (excludes warmup)")
    ap.add_argument("--warmup", type=float, default=2.0, help="Warm-up seconds before measurement")
    ap.add_argument("--concurrency", type=int, default=50, help="Max in-flight requests")
    ap.add_argument("--rps", type=float, default=0.0, help="Target req/s (open-loop). If 0, run closed-loop at max throughput.")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--http2", action="store_true", help="Enable HTTP/2 if server supports it")
    ap.add_argument("--progress", action="store_true", help="Print 1s progress")
    args = ap.parse_args()

    headers = parse_headers(args.header)
    body = args.body.encode() if args.body is not None else None

    limits = httpx.Limits(
        max_keepalive_connections=max(10, args.concurrency),
        max_connections=max(100, args.concurrency * 2),
    )
    async with httpx.AsyncClient(http2=args.http2, limits=limits) as client:
        if args.warmup > 0:
            print(f"Warm-up {args.warmup}s …")
            await warmup(client, args.method, args.url, headers, body, args.warmup, min(args.concurrency, 16), args.timeout)

        print(f"Running {'open-loop' if args.rps > 0 else 'closed-loop'} test for {args.duration:.0f}s "
              f"({'%.1f RPS' % args.rps if args.rps>0 else f'concurrency={args.concurrency}'}) …")
        t0 = time.perf_counter()
        if args.rps > 0:
            stats = await run_open_loop(client, args.method, args.url, headers, body,
                                        args.duration, args.rps, args.concurrency, args.timeout, progress=args.progress)
        else:
            stats = await run_closed_loop(client, args.method, args.url, headers, body,
                                          args.duration, args.concurrency, args.timeout, progress=args.progress)
        elapsed = max(0.001, time.perf_counter() - t0)
        summarize(stats, elapsed)

if __name__ == "__main__":
    asyncio.run(main())
