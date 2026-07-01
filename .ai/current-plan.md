# Plan — Manual paste uses G2G offer title

## Goal

For G2G manual paste only, send the full marketplace `offer_title` as `itemName`; keep automatic scanner mapping unchanged.

## Allowed files

- `scanners/g2g_scanner_api.py`
- `scanners/main.py`
- focused tests under `tests/`
- `docs/manual_paste.md`
- `.ai/current-plan.md`

## Do not touch

- Automatic scan title mapping and filters.
- Marketplace state transitions, pricing, auth, worker delivery, or `.env`.

## Steps

1. Add an explicit extraction option that prefers `offer_title` only for manual paste.
2. Pass that option from `handle_manual_paste`; default scan/recovery behavior stays unchanged.
3. Add focused tests proving manual and automatic paths produce different titles.
4. Update manual-paste docs and run syntax/tests.

## Acceptance

- Manual paste payload `itemName` equals the full non-empty G2G `offer_title`.
- Empty titles retain the current `Item Type - detail` fallback.
- Automatic scanner payload remains `Gear - Amulet` for the same raw API response.
