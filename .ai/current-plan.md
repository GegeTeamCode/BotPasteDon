# Plan — G2G proof upload: stop misclassifying S3/network failures as terminal

> Refresh 2026-08-31: điều tra lại sau khi lỗi tái phát 30–31/8 (đơn TQL6 của
> Chồng). Xác nhận bug KHÔNG phải 1-off: **7 đơn từ 27/8**, 6 đơn còn kẹt trải trên
> 2 ERP (100 + 102). Thêm Phase A data-ops re-push. Thiết kế fix code (Phase B)
> giữ nguyên bản 29/8.

## Goal

When every proof file fails to upload for transient reasons (S3 connection
errors, upload HTTP failure, missing presigned URL), the worker must retry.
Only genuinely unsupported file types (webp/heic/no-ext) stay terminal.

## Root cause — verified bằng log + code (2026-08-31)

`shared/g2g_api.py::_upload_proofs` (local HEAD `38fad07` = bản đang chạy trên
.220, md5 khớp):

- Một list `skipped` gom CẢ ext-unsupported lẫn network fail (no presigned URL,
  `upload_to_s3` trả False, per-file exception).
- Khi 0 file upload được → raise APIError
  `"delivery_proof: all N proof file(s) unsupported file type, manual upload
  needed"` — `_classify_error` (workers/g2g_worker.py) match keyword →
  **terminal** → ORDER_FAILED, no retry, ERP không được báo gì.

Log mới nhất (TQL6, 31/8 20:15, /tmp/g2g_worker.log):

```
20:15:04 Downloaded 1/1 files from ERP          ← mp4 tải về OK
20:15:08 delivered_qty=650 OK                    ← qty submit OK
20:15:15 Proof upload failed ... Max retries exceeded (S3 G2G)   ← lỗi MẠNG
20:15:18 ERROR: Terminal error ... unsupported file type        ← NHÃN SAI
```

## Danh sách 7 đơn dính lỗi (27/8–31/8)

| ERP | Sell Order | G2G order | State ERP | Trạng thái |
|---|---|---|---|---|
| 102 | SO-260828-09PD6CDY | 1787925452427W9GN | Delivered | **ĐÃ re-push OK 29/8** (Completed sau 5s) |
| 100 | SO-260827-1HJF161M | 1787831231849FEO1 | Completed | KẸT |
| 100 | SO-260828-M5U1O9XU | 1787899588255PBP2 | Completed | KẸT |
| 100 | SO-260828-H5V0VKWX | 1787924436466W2W4 | Delivered | KẸT |
| 100 | SO-260830-RD6QB3G5 | 1788073856716YNO3 | Completed | KẸT |
| 102 | SO-260831-3IXXNXTJ | 1788152346102SU4X | Delivered | KẸT |
| 102 | SO-260831-J9JKWD4L | 1788181237562TQL6 | Completed | KẸT |

Mỗi đơn có đúng 1 evidence file mp4, qty đã submit trên G2G — chỉ thiếu proof.
ERP side đã đi hết flow (Delivered/Completed + ALE) nên KHÔNG đụng gì ERP.

## Phase A — Data ops: re-push 6 đơn kẹt (chữa cháy, chạy trước/độc lập fix code)

- Dùng `scripts/retry_post_evidence.py` (đã có, từng dùng 29/8 cho W9GN):
  script tự SSH ERP tra SO → gọi `post_evidence_to_marketplace(skip_steps=
  '["qty"]')` → tail worker log 90s chờ `Completed: <order_id>`.
- ⚠️ Script đang **hardcode ERP .100** (`ERP_HOST = "192.168.2.100"`): 4 đơn
  chạy thẳng được; 2 đơn trên 102 (3IXXNXTJ, J9JKWD4L) cần thêm param host/site
  (đưa vào Allowed files) hoặc chạy tay `bench execute` trên 102.
- `WorkflowTransitionError` bắn ra SAU khi worker nhận task là benign (SO đã
  terminal state) — docstring script đã ghi.
- Verify sau re-push: worker log có `Completed:`; G2G dashboard order →
  Delivery/Evidence tab thấy file.

## Phase B — Code fix (thiết kế giữ nguyên 29/8)

1. Split per-file skip bookkeeping trong `_upload_proofs` thành 2 list:
   `unsupported` (ext không thuộc `_G2G_PROOF_EXTS`) và `failed` (no presigned
   URL, S3 upload False, per-file exception; AuthError vẫn bubble lên nguyên
   vẹn).
2. Khi 0 file upload được, raise theo thứ tự ưu tiên:
   a. `failed` non-empty → APIError message nhúng nguyên văn lỗi underneath
      ("max retries exceeded"/…) để `_classify_error` xếp network/retryable.
      Message KHÔNG chứa "unsupported".
   b. else `unsupported` → giữ NGUYÊN VĂN terminal message như hiện tại.
3. Mixed (vài file ext xấu + vài file network fail): ≥1 file upload được thì
   submit như hiện nay; 0 file thì ưu tiên `failed` để bước này được retry —
   các file thực sự unsupported chỉ terminal khi chúng là phần còn lại duy nhất.
4. `py_compile` + chạy lại 1 chu kỳ giao hàng thật, xem /tmp/g2g_worker.log.

## Allowed files

- `shared/g2g_api.py` (`_upload_proofs` only)
- `scripts/retry_post_evidence.py` (chỉ thêm param host/site cho Phase A)
- `.ai/current-plan.md`, `.ai/decisions.md`, `.ai/task-log.md`

## Do not touch

- `_TERMINAL_KEYWORDS` / `_classify_error` in workers/g2g_worker.py — the
  fix is sending the right message, not loosening classification.
- The terminal message text itself (webp/heic case must keep matching
  "proof file(s) unsupported").
- `upload_to_s3` internals, submit path, worker retry loop, coordinator,
  scanners, auth.
- `.env`, `data/orders.db` rows, chrome profiles.

## Deploy + verify (Phase B)

1. `py_compile` syntax check, commit, `git push origin main`, deploy via
   `scripts/deploy_git.py`.
2. Watch one delivery cycle in /tmp/g2g_worker.log.

## Acceptance

- A network/S3 proof-upload failure logs "… failure (attempt N, next in 60s)"
  and marks RETRY_PENDING — no "Terminal error" line.
- webp/heic/no-ext files still raise the terminal message verbatim.
- Orders with ≥1 successfully uploaded file behave exactly as before.
- Normal deliveries still log "Completed:" after deploy.
- Phase A: cả 6 đơn log `Completed:` trên worker, evidence hiện trên G2G
  dashboard.

## Risks

- ERP `delivery_callback` is deliberately OUT of this patch: the ERP endpoint
  flips the Sell Order to "Disputed" on success=False, which is wrong for
  transient failures that later succeed on retry (and mutating ERP
  workflow_state is out-of-scope per AGENTS.md). Needs its own design later
  (log-only action, no state flip).
- If S3 stays down past 100 retries (~4 days of backoff), the order ends
  ORDER_FAILED with RETRY_CAP — visible in the bot DB/dashboard, no silent
  loss.
- Message wording drift: the new "failed to upload" error text must never
  contain the substrings in `_TERMINAL_KEYWORDS`.
- Phase A re-push khi S3 vẫn lag → task sẽ fail lại y hệt (lỗi chưa fix):
  re-push sau khi deploy Phase B thì an toàn hơn, hoặc re-push chọn lúc
  mạng ổn.
