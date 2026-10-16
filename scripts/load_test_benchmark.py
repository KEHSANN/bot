#!/usr/bin/env python3
"""
Reproducible Load & Capacity Benchmark Suite (Section 24).
Executes realistic concurrent user journeys (onboarding, cached membership check,
quote generation, wallet credit, and transactional purchase) and measures actual throughput
and latency percentiles (p50, p95, p99).
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import json
import statistics
import time

from backend.app.bot.telegram_bot import ApplicationContainer, TelegramBotDispatcher


def run_benchmark(num_users: int = 200, concurrency: int = 16) -> dict:
    container = ApplicationContainer(db_path=":memory:")
    bot = TelegramBotDispatcher(container)
    admin = container.rbac.bootstrap_super_admin(999000, "bench_admin", "StrongPass!123")
    cat = container.catalog.create_category(
        admin["id"], slug="bench", title_fa="بanch", description_fa="تست بار"
    )
    prod = container.catalog.create_product(
        admin["id"],
        category_id=cat["id"],
        code="bench_auto",
        title_fa="اشتراک تست بار",
        description_fa="خودکار",
        service_type="vless",
        inventory_mode="automated_provider",
        provider_code="fake_marzban_dev",
        base_price_irt=50000,
        min_random_increment_irt=100,
        max_random_increment_irt=500,
        increment_step_irt=100,
        duration_days=30,
        traffic_gb=40,
        device_limit=2,
    )

    latencies_ms = []
    errors = 0

    def _user_journey(idx: int) -> float:
        t0 = time.perf_counter()
        tg_id = 100000 + idx
        start_res = bot.handle_start(tg_id, f"کاربر {idx}")
        uid = start_res["user"]["id"]
        container.wallet.admin_adjust_wallet(
            admin["id"], uid, 100000, "Benchmark seed", f"bench_seed_{idx}"
        )
        q_res = bot.handle_customer_quote(tg_id, prod["id"])
        buy_res = bot.handle_customer_purchase(
            tg_id, q_res["quote"]["id"], f"bench_buy_{idx}"
        )
        if buy_res["screen"] != "purchase_success":
            raise RuntimeError("Purchase failed during load test")
        return (time.perf_counter() - t0) * 1000.0

    start_wall = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for future in [pool.submit(_user_journey, i) for i in range(num_users)]:
            try:
                latencies_ms.append(future.result())
            except Exception:
                errors += 1
    elapsed_sec = time.perf_counter() - start_wall
    latencies_ms.sort()

    def _pct(p: float) -> float:
        if not latencies_ms:
            return 0.0
        k = min(len(latencies_ms) - 1, int(round((p / 100.0) * (len(latencies_ms) - 1))))
        return round(latencies_ms[k], 2)

    report = {
        "tested_journeys": num_users,
        "concurrency": concurrency,
        "total_duration_seconds": round(elapsed_sec, 3),
        "journeys_per_second": round(num_users / elapsed_sec, 2),
        "errors": errors,
        "latency_ms": {
            "mean": round(statistics.mean(latencies_ms), 2),
            "p50": _pct(50),
            "p95": _pct(95),
            "p99": _pct(99),
        },
    }
    return report


if __name__ == "__main__":
    result = run_benchmark()
    print(json.dumps(result, indent=2, ensure_ascii=False))
