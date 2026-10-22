# p10: tao MOT offer Aion 2 Kinah tren Eldorado (POST predefinedOffers) qua ElBudget,
# dung thu tu worker/tasks/el_task.py::_post_write:
#   reserve_post -> POST -> log -> on_post_success | on_post_429 (dung) | refund_post (chi loi xac thuc)
# Cach chay:  p10_post_one.py <env_id> <ton> <min>            (chay kho: in payload, khong POST)
#             p10_post_one.py <env_id> <ton> <min> --post     (POST that)
# Rao: board doc lai ngay truoc khi POST; /me phai KHONG co offer nao cua minh o to hop nay;
#      pool >= 200, khong co khoa 429; moi request ghi vao writes.jsonl (khong token).
import asyncio
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("EL_WRITE_LOG", str(HERE / "el_writes_local.jsonl"))
os.environ.setdefault("WORKER_ID", "c199-manual-aion2")

import elcommon as E  # noqa: E402  (nap .env, sys.path repo)
from elcommon import get, dump  # noqa: E402

import redis.asyncio as aioredis  # noqa: E402
from shared.config.eldorado import CREATE_OFFER_URL, EldoradoCurrency  # noqa: E402
from worker.el_budget import K_LEDGER, POOL_WINDOW, ElBudget  # noqa: E402
from worker.el_writelog import log_write  # noqa: E402
from worker.updater.eldorado import EldoradoUpdater  # noqa: E402

STEP = Decimal("0.00001")
POOL_FLOOR = 200
DESC = (
    "Welcome to GEGEShop!\n"
    "1 unit = 1,000,000 Kinah (1M).\n\n"
    "Delivery via Auction House: after ordering, please message us in the order chat. "
    "We will tell you which item to list and at what price, then buy it to send your Kinah.\n"
    "Auction House tax is not covered.\n\n"
    "100% handmade, no bot.\n"
    "Our team is online 24/7 - message us anytime."
)
DELIVERY_TIME = "Hour1"
DELIVERY_METHOD = "AuctionHouse"

env_id, stock, min_qty = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
DO_POST = "--post" in sys.argv
combos = {c["env_id"]: c for c in json.loads((HERE / "combos.json").read_text())}
combo = combos[env_id]
label = f"{combo['region']} / {combo['faction']} / {combo['server']}"


def wlog(rec: dict):
    rec = dict(rec, ts=time.strftime("%Y-%m-%dT%H:%M:%S"), epoch=round(time.time(), 3))
    with open(HERE / "writes.jsonl", "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def price_rank8(rows):
    rows = [r for r in rows if (r["user"]["username"] or "").lower() != "gegeteam"]
    ps = sorted(Decimal(repr(r["offer"]["pricePerUnit"]["amount"])).quantize(STEP) for r in rows)
    if len(ps) < 8:
        raise SystemExit(f"board {env_id} chi co {len(ps)} dong - offer thu can to hop >= 8 dong")
    p7, p8 = ps[6], ps[7]
    p = p8 - STEP
    tie = p <= p7
    if tie:
        p = p8
    return p, p7, p8, tie, len(ps)


async def main():
    # 1) Board ngay luc nay
    st, d = get("predefinedOffers/augmentedGame/offers",
                {"gameId": "360", "category": "Currency", "tradeEnvironmentId": env_id,
                 "pageIndex": 1, "pageSize": 100})
    assert st == 200 and d.get("recordCount", 0) <= 100, (st, str(d)[:200])
    dump(f"board_{env_id}_pre.json", d)
    price, p7, p8, tie, n = price_rank8(d["results"])
    print(f"[{label}] board {n} dong, dong7={p7} dong8={p8} -> gia {price}{' (hoa gia)' if tie else ''}")

    # 2) /me: to hop nay va ca game 360 phai trong
    st1, m1 = get("predefinedOffers/me", {"gameId": "360", "category": "Currency",
                                          "tradeEnvironmentId": env_id, "pageIndex": 1,
                                          "pageSize": 50}, auth=True)
    st2, m2 = get("predefinedOffers/me", {"gameId": "360", "category": "Currency",
                                          "pageIndex": 1, "pageSize": 50}, auth=True)
    print("/me env", st1, m1.get("recordCount"), "| /me game360", st2, m2.get("recordCount"))
    if st1 != 200 or st2 != 200 or m1.get("recordCount") != 0 or m2.get("recordCount") != 0:
        raise SystemExit("DUNG: /me loi hoac da co offer cua minh o game 360 - khong POST")

    # 3) Ngan sach POST
    r = aioredis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                          decode_responses=True)
    budget = ElBudget(r)
    now0 = time.time()
    pool = await budget.pool_remaining(now0)
    anchor, stage = await budget.lock_state()
    gate0 = await budget.check_post(now0)
    ledger0 = await r.zcount(K_LEDGER, now0 - POOL_WINDOW, "+inf")
    print(f"pool_remaining={pool} ledger_12h={ledger0} lock_anchor={anchor} stage={stage} "
          f"check_post={gate0.allowed}/{gate0.reason} post_60s={gate0.post_in_60s} wait={gate0.wait:.1f}")
    if pool < POOL_FLOOR or anchor is not None:
        raise SystemExit("DUNG: pool < 200 hoac dang khoa 429")

    coin = EldoradoCurrency(
        key="aion2kinah_manual", name="Kinah", league=label, gameId="360",
        augmented_game_id="", trade_env_id=env_id, attribute_ids="",
        description=DESC, delivery_time=DELIVERY_TIME, delivery_method=DELIVERY_METHOD)
    payload = coin.build_payload(float(price), quantity=stock, min_quantity=min_qty)
    print("payload:", json.dumps(payload, ensure_ascii=False))
    if not DO_POST:
        print("CHAY KHO - khong POST")
        await r.aclose()
        return

    # 4) reserve_post -> POST (dung thu tu el_task)
    for _ in range(3):
        gate = await budget.check_post()
        if gate.allowed or gate.reason not in ("post_min_gap", "post_rate_60s"):
            break
        print(f"cho {gate.reason} {gate.wait:.1f}s")
        await asyncio.sleep(gate.wait + 0.5)
    reserved_at = time.time()
    gate = await budget.reserve_post(reserved_at)
    if not gate.allowed:
        await log_write(r, coin=coin.key, method="POST", price=float(price),
                        status=f"skipped:{gate.reason}", pool_remaining=gate.pool_remaining,
                        post_in_60s=gate.post_in_60s, delete_in_60s=gate.delete_in_60s,
                        reason="manual_aion2_test")
        raise SystemExit(f"DUNG: ElBudget tu choi ({gate.reason})")
    print(f"reserve_post OK: pool con {gate.pool_remaining}, member ledger {reserved_at:.3f}")

    headers = EldoradoUpdater._profile_headers({"bearer_token": E.token(), "user_agent": E.UA})
    upd = EldoradoUpdater(coin, impersonate="chrome131")
    E._throttle()
    res = upd.post_create(headers, float(price), stock, min_qty)
    wlog({"method": "POST", "url": CREATE_OFFER_URL, "payload": payload, "status": res.status,
          "latency_ms": res.latency_ms, "body": res.body[:4000], "offer_id": res.offer_id,
          "env_id": env_id, "label": label, "pool_after_reserve": gate.pool_remaining,
          "ledger_member": f"{reserved_at:.3f}"})
    await log_write(r, coin=coin.key, method=res.method_name, offer_id=res.offer_id,
                    status=res.status, latency_ms=res.latency_ms, price=float(price),
                    offer_version=res.version, pool_remaining=gate.pool_remaining,
                    post_in_60s=gate.post_in_60s, delete_in_60s=gate.delete_in_60s,
                    reason=f"manual_aion2_test {env_id}", body=res.body,
                    extra={"new_offer_id": res.offer_id} if res.offer_id else None)
    print("POST ->", res.status, f"{res.latency_ms}ms", "offer_id", res.offer_id,
          "version", res.version)
    print("body:", res.body[:1500])

    if res.ok:
        if await budget.on_post_success():
            print("(POST nay ket thuc mot khoa 429 cu)")
    elif res.rate_limited:
        stg, nxt = await budget.on_post_429()
        print(f"POST 429 -> khoa stage={stg}, thu lai luc {nxt}. DUNG MOI POST.")
    elif res.auth_error:
        await budget.refund_post(reserved_at)
        print("loi xac thuc -> da hoan suat (refund_post)")
    else:
        print("POST loi khac -> giu nguyen suat da giu (nhu el_task)")

    now1 = time.time()
    ledger1 = await r.zcount(K_LEDGER, now1 - POOL_WINDOW, "+inf")
    mine = await r.zscore(K_LEDGER, f"{reserved_at:.3f}")
    print(f"ledger_12h {ledger0} -> {ledger1}; member cua minh trong ledger: {mine is not None}; "
          f"pool_remaining sau = {await budget.pool_remaining(now1)}")
    await r.aclose()


E.token()  # xin/doc token truoc (token() dung asyncio.run rieng, khong goi duoc trong main)
asyncio.run(main())
