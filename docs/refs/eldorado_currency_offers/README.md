# Script tạo offer currency Eldorado (bản đã chạy cho Aion 2, 22–23/10/2026)

Bản sao nguyên văn các script đã chạy trên `.199` (`~/el_aion2_20261022/`) để tạo 104 offer Aion 2 Kinah.
Không phải mã của BotPasteDon và không được deploy hay chạy trên `.220`. Chúng import mã của bot đạp giá
`G2G-AutomationBot-v4` (`ElBudget`, `control/auth/el_seller_api`, `EldoradoUpdater`), nên chỉ chạy trên `.199`
với `PYTHONPATH` của repo đó.

Cách dùng, thứ tự chạy và các chỗ phải sửa cho game khác: [../../marketplace_offers.md](../../marketplace_offers.md) mục 6.

| Tệp | Việc | Ghi |
|---|---|---|
| `elcommon.py` | GET có nhịp 1,1 s (khoá file, dừng ở 429 đầu), token Seller API | — |
| `p01_library.py` | Đọc `/library`, tìm game | — |
| `p06_tree.py` | Cây env `library/{gameId}/{category}` → `tree.json` | — |
| `p08_scan.py` | Board mọi tổ hợp, `/me` + `stateCount`, offer G2G của GeGeTeam | — |
| `p09_dryrun.py` | Bảng dry-run giá hạng 8 (chỉ đọc tệp) | — |
| `p10_post_one.py` | Tạo 1 offer qua `ElBudget` (chạy khô nếu thiếu `--post`) | **POST** |
| `p11_verify.py` | Đo lại 1 offer + hạng trên board | — |
| `p12_create_all.py` | Tạo hết theo bảng dry-run, rồi đo lại | **POST** |
