# p12: tao het cac offer Aion 2 Kinah con lai theo bang dry-run da duyet (22/10/2026).
# Moi to hop: doc lai board -> tinh gia hang 8 (cung luat p09) -> /me to hop phai trong ->
# ElBudget.reserve_post -> POST predefinedOffers -> on_post_success | on_post_429 (DUNG) | refund (loi xac thuc, DUNG).
# Nhip: >= 8s giua hai POST. Dung khi: 429, "Maximum of", 2 loi lien tiep, pool < 200, offersUpdatedCount > 0.
# Cuoi: doc lai board tung to hop de do Active/gia/ton/min/hang; ghi DONE marker.
#   p12_create_all.py [--limit N]
import asyncio
import csv
import json
import os
import statistics
import sys
import time
import traceback
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("EL_WRITE_LOG", str(HERE / "el_writes_local.jsonl"))
os.environ.setdefault("WORKER_ID", "c199-manual-aion2")

import elcommon as E  # noqa: E402
from elcommon import GetStop, dump, get  # noqa: E402

import redis.asyncio as aioredis  # noqa: E402
from control.auth import el_seller_api  # noqa: E402
from shared.config.eldorado import CREATE_OFFER_URL, EldoradoCurrency  # noqa: E402
from worker.el_budget import K_LEDGER, POOL_WINDOW, ElBudget  # noqa: E402
from worker.el_writelog import log_write  # noqa: E402
from worker.updater.eldorado import EldoradoUpdater  # noqa: E402

STEP = Decimal("0.00001")
OUTLIER_X = Decimal(2)
POOL_FLOOR = 200
POST_GAP = 8.0
TEST_ENV = "2-9-5"
DESC = (
    "Welcome to GEGEShop!\n"
    "1 unit = 1,000,000 Kinah (1M).\n\n"
    "Delivery via Auction House: after ordering, please message us in the order chat. "
    "We will tell you which item to list and at what price, then buy it to send your Kinah.\n"
    "Auction House tax is not covered.\n\n"
    "100% handmade, no bot.\n"
    "Our team is online 24/7 - message us anytime."
)
DELIVERY_TIME, DELIVERY_METHOD = "Hour1", "AuctionHouse"
LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
DONE = HERE / "run_all.done"
CREATED = HERE / "created.jsonl"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def wlog(rec):
    rec = dict(rec, ts=time.strftime("%Y-%m-%dT%H:%M:%S"), epoch=round(time.time(), 3))
    with open(HERE / "writes.jsonl", "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def dec(x):
    return Decimal(repr(x)).quantize(STEP)


def compute_price(rows):
    """Cung luat p09. Tra (gia|None, case, rank, ghi chu)."""
    rows = [r for r in rows if (r["user"]["username"] or "").lower() != "gegeteam"]
    ps = sorted(dec(r["offer"]["pricePerUnit"]["amount"]) for r in rows)
    n = len(ps)
    if n == 0:
        return None, "trong", "", "bang trong"
    med = statistics.median(ps)
    note = ""
    if n >= 8:
        p7, p8 = ps[6], ps[7]
        p = p8 - STEP
        if p <= p7:
            p = p8
            note = f"hoa gia dong8={p8} dong7={p7}"
        case = ">=8"
    else:
        keep = [x for x in ps if x <= OUTLIER_X * med]
        p = max(keep) + STEP
        case = "<8"
        note = f"duoi 8 dong, bo {n - len(keep)} dong > 2x trung vi"
    lo = 1 + sum(1 for x in ps if x < p)
    hi = 1 + sum(1 for x in ps if x <= p)
    return p, case, (str(lo) if lo == hi else f"{lo}-{hi}"), note


async def ensure_token(redis):
    if E._tok["t"] and E._tok["exp"] - time.time() > 150:
        return
    b = await el_seller_api.get_bundle(redis)
    if not b or not b.get("bearer_token"):
        raise RuntimeError(f"token Seller API loi: {el_seller_api.last_error()}")
    E._tok["t"], E._tok["exp"] = b["bearer_token"], float(b["expires_at"])


def read_board(env):
    rows, page = [], 1
    while True:
        st, d = get("predefinedOffers/augmentedGame/offers",
                    {"gameId": "360", "category": "Currency", "tradeEnvironmentId": env,
                     "pageIndex": page, "pageSize": 100})
        if st != 200 or not isinstance(d, dict):
            raise RuntimeError(f"board {env} -> {st} {str(d)[:200]}")
        rows += d.get("results") or []
        if page * 100 >= (d.get("recordCount") or 0):
            return rows
        page += 1


def me_env(env):
    st, d = get("predefinedOffers/me", {"gameId": "360", "category": "Currency",
                                        "tradeEnvironmentId": env, "pageIndex": 1,
                                        "pageSize": 50}, auth=True)
    if st != 200 or not isinstance(d, dict):
        raise RuntimeError(f"/me {env} -> {st} {str(d)[:200]}")
    return d.get("recordCount"), d.get("results") or []


async def main():
    r = aioredis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                          decode_responses=True)
    budget = ElBudget(r)
    plan = [row for row in csv.DictReader(open(HERE / "dryrun_aion2_el.csv"))
            if row["env_id"] != TEST_ENV and row["proposed"]]
    if LIMIT:
        plan = plan[:LIMIT]
    log(f"KE HOACH: {len(plan)} to hop (bo {TEST_ENV} = offer thu da co)")

    await ensure_token(r)
    # Kiem bo loc /me theo env: to hop offer thu phai ra 1, to hop khac ra 0.
    n_test, _ = me_env(TEST_ENV)
    n_other, _ = me_env(plan[-1]["env_id"])
    log(f"/me loc env: {TEST_ENV} -> {n_test}, {plan[-1]['env_id']} -> {n_other}")
    if n_test != 1 or n_other != 0:
        raise SystemExit("DUNG: bo loc /me theo env khong dang tin")

    now0 = time.time()
    ledger0 = await r.zcount(K_LEDGER, now0 - POOL_WINDOW, "+inf")
    pool0 = await budget.pool_remaining(now0)
    log(f"pool_remaining={pool0} ledger_12h={ledger0}")

    upd = EldoradoUpdater(EldoradoCurrency(key="aion2kinah_manual", name="Kinah", league="",
                                           gameId="360", augmented_game_id="", trade_env_id="",
                                           attribute_ids=""),
                          impersonate="chrome131")
    last_post = 0.0
    fails = 0
    created, skipped = [], []
    stop_reason = None
    for i, row in enumerate(plan, 1):
        env = row["env_id"]
        label = f"{row['region']}/{row['faction']}/{row['server']}"
        stock, mn = int(row["stock"]), int(row["min"])
        try:
            rows = read_board(env)
            price, case, rank, note = compute_price(rows)
            if price is None:
                skipped.append((env, "bang trong"))
                log(f"[{i}/{len(plan)}] {label} {env}: bang trong -> bo qua")
                continue
            await ensure_token(r)
            n_me, _ = me_env(env)
            if n_me != 0:
                skipped.append((env, f"da co {n_me} offer cua minh"))
                log(f"[{i}/{len(plan)}] {label} {env}: DA CO offer cua minh -> khong POST")
                continue
        except (GetStop, RuntimeError) as e:
            stop_reason = f"{type(e).__name__}: {e}"
            break

        pool = await budget.pool_remaining()
        anchor, _ = await budget.lock_state()
        if pool < POOL_FLOOR or anchor is not None:
            stop_reason = f"pool={pool} lock_anchor={anchor}"
            break
        wait = last_post + POST_GAP - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        for _ in range(6):
            gate = await budget.check_post()
            if gate.allowed or gate.reason not in ("post_min_gap", "post_rate_60s"):
                break
            log(f"   cho {gate.reason} {gate.wait:.1f}s")
            await asyncio.sleep(gate.wait + 0.5)
        reserved_at = time.time()
        gate = await budget.reserve_post(reserved_at)
        if not gate.allowed:
            await log_write(r, coin="aion2kinah_manual", method="POST", price=float(price),
                            status=f"skipped:{gate.reason}", pool_remaining=gate.pool_remaining,
                            post_in_60s=gate.post_in_60s, delete_in_60s=gate.delete_in_60s,
                            reason=f"manual_aion2_bulk {env}")
            stop_reason = f"ElBudget tu choi: {gate.reason}"
            break
        last_post = reserved_at

        upd.coin = EldoradoCurrency(
            key="aion2kinah_manual", name="Kinah", league=label, gameId="360",
            augmented_game_id="", trade_env_id=env, attribute_ids="",
            description=DESC, delivery_time=DELIVERY_TIME, delivery_method=DELIVERY_METHOD)
        payload = upd.coin.build_payload(float(price), quantity=stock, min_quantity=mn)
        headers = EldoradoUpdater._profile_headers({"bearer_token": E._tok["t"], "user_agent": E.UA})
        E._throttle()
        res = upd.post_create(headers, float(price), stock, mn)
        wlog({"method": "POST", "url": CREATE_OFFER_URL, "payload": payload, "status": res.status,
              "latency_ms": res.latency_ms, "body": res.body[:4000], "offer_id": res.offer_id,
              "env_id": env, "label": label, "pool_after_reserve": gate.pool_remaining,
              "ledger_member": f"{reserved_at:.3f}"})
        await log_write(r, coin="aion2kinah_manual", method=res.method_name, offer_id=res.offer_id,
                        status=res.status, latency_ms=res.latency_ms, price=float(price),
                        offer_version=res.version, pool_remaining=gate.pool_remaining,
                        post_in_60s=gate.post_in_60s, delete_in_60s=gate.delete_in_60s,
                        reason=f"manual_aion2_bulk {env}", body=res.body,
                        extra={"new_offer_id": res.offer_id} if res.offer_id else None)

        if res.ok:
            await budget.on_post_success()
            fails = 0
            try:
                body = json.loads(res.body)
            except Exception:
                body = {}
            o = body.get("offer") or {}
            env_back = [t.get("id") for t in o.get("tradeEnvironmentValues") or []]
            rec = {"env_id": env, "label": label, "offer_id": res.offer_id, "price": str(price),
                   "table_price": row["proposed"], "case": case, "rank_expected": rank,
                   "note": note, "stock": stock, "min": mn,
                   "created": body.get("offersCreatedCount"), "updated": body.get("offersUpdatedCount"),
                   "env_back": env_back, "qty_back": o.get("quantity"), "min_back": o.get("minQuantity"),
                   "price_back": (o.get("pricePerUnit") or {}).get("amount"),
                   "unit_back": o.get("unitSystem"), "state_back": o.get("offerState"),
                   "expire_back": o.get("expireDate")}
            with open(CREATED, "a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            created.append(rec)
            log(f"[{i}/{len(plan)}] {label} {env}: 201 {res.offer_id} gia {price} (bang {row['proposed']}) "
                f"hang {rank} ton {stock} min {mn} pool {gate.pool_remaining} {note}")
            if (body.get("offersUpdatedCount") or 0) > 0 or body.get("offersCreatedCount") != 1:
                stop_reason = f"POST {env} tra offersUpdatedCount={body.get('offersUpdatedCount')} " \
                              f"created={body.get('offersCreatedCount')} - DUNG"
                break
            if env_back and env_back[-1] != env:
                stop_reason = f"env tra ve {env_back} khac {env} - DUNG"
                break
            continue

        log(f"[{i}/{len(plan)}] {label} {env}: POST {res.status} {res.body[:300]}")
        if res.rate_limited:
            stg, nxt = await budget.on_post_429()
            stop_reason = f"POST 429 (khoa stage={stg}, thu lai {nxt})"
            break
        if res.auth_error:
            await budget.refund_post(reserved_at)
            stop_reason = f"loi xac thuc {res.status} (da refund)"
            break
        if "Maximum of" in (res.body or ""):
            stop_reason = f"tran offer active: {res.body[:300]}"
            break
        fails += 1
        skipped.append((env, f"POST {res.status}"))
        if fails >= 2:
            stop_reason = "2 loi POST lien tiep"
            break

    now1 = time.time()
    ledger1 = await r.zcount(K_LEDGER, now1 - POOL_WINDOW, "+inf")
    log(f"KET THUC TAO: tao {len(created)}, bo qua {len(skipped)} {skipped}, dung: {stop_reason}")
    log(f"ledger_12h {ledger0} -> {ledger1} (tang {ledger1 - ledger0}); pool "
        f"{pool0} -> {await budget.pool_remaining(now1)}")
    await r.aclose()

    # Do lai: doc board tung to hop vua tao.
    log("DO LAI board tung offer ...")
    verify = []
    try:
        for c in created:
            rows = read_board(c["env_id"])
            rows.sort(key=lambda x: x["offer"]["pricePerUnit"]["amount"])
            idx = [k for k, x in enumerate(rows) if x["offer"]["id"] == c["offer_id"]]
            v = {"env_id": c["env_id"], "offer_id": c["offer_id"], "on_board": bool(idx)}
            if idx:
                o = rows[idx[0]]["offer"]
                p = o["pricePerUnit"]["amount"]
                v.update(rank_price=1 + sum(1 for x in rows if x["offer"]["pricePerUnit"]["amount"] < p
                                            and x["offer"]["id"] != c["offer_id"]),
                         rank_pos=idx[0] + 1, rows=len(rows), state=o.get("offerState"),
                         price=p, qty=o.get("quantity"), min=o.get("minQuantity"),
                         match=(dec(p) == Decimal(c["price"]) and o.get("quantity") == c["stock"]
                                and o.get("minQuantity") == c["min"]))
            else:
                st, d = get(f"predefinedOffers/{c['offer_id']}")
                oo = (d or {}).get("offer") or {} if isinstance(d, dict) else {}
                v.update(get_status=st, state=oo.get("offerState"))
            verify.append(v)
    except GetStop as e:
        log("DO LAI dung vi", e)
    dump("verify_all.json", verify)
    not_on = [v for v in verify if not v["on_board"]]
    mism = [v for v in verify if v["on_board"] and not v["match"]]
    log(f"DO LAI: {len(verify)} offer, tren board {len(verify) - len(not_on)}, khong thay {len(not_on)}, "
        f"lech gia/ton/min {len(mism)}")


try:
    asyncio.run(main())
except Exception:
    traceback.print_exc()
finally:
    DONE.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
