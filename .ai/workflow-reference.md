# Workflow Reference — BotPasteDon

*Bản rút gọn project-specific của `DAILY-WORKFLOW.md` + `WORKED-EXAMPLES.md`.
Chi tiết đầy đủ xem nguồn tại `D:\AI vibe coding\`.*

---

## 1. Bản đồ tool / model

| Việc | Công cụ | Model |
|---|---|---|
| Lập plan, task dài, code khó | Zoo Code — **Architect** | `glm-5.1` (chạy off-peak) |
| Implement chính | Zoo Code — **Code** | `glm-4.7` |
| Fix bug | Zoo Code — **Debug** | `glm-4.7` → `glm-5-turbo` khi khó |
| Hỏi đáp, scout, docs, commit msg | Zoo Code — **Ask** | `glm-4.5-air` |
| Soát diff rẻ (first-pass) | Zoo Code — **Reviewer** (read-only) | `glm-5.1` |
| Review cuối, khác họ model | **Claude Code** (`/ultrareview`) | **Opus (Pro)** |

**Off-peak** = ngoài khung **13:00–17:00 giờ VN** (14:00–18:00 UTC+8).

---

## 2. Cấu trúc file `.ai/` (đã có)

```
.ai/
├── project-context.md    ← Dự án là gì, stack, topology
├── architecture.md        ← 9 services, data flow, sensitive areas
├── coding-standards.md    ← Quy ước code + model + review rules
├── test-commands.md       ← Lệnh test/verify chính xác
├── current-plan.md        ← Plan của task đang làm (Architect ghi)
├── task-log.md            ← Nhật ký việc đã làm (append-only)
├── decisions.md           ← Quyết định kỹ thuật quan trọng + lý do
├── spec.md                ← Contract bất biến (status FSM, webhook payload, auth)
├── handoff.md             ← Tóm tắt khi đóng session
└── workflow-reference.md  ← FILE NÀY — prompt library + daily workflow
```

---

## 3. Vòng lặp task hằng ngày

**Một session = một mục tiêu.**

### Bước 1 — PLAN (mode Architect, glm-5.1, off-peak)

```
Task:
<mô tả task cụ thể, vd: "fix g2g_scanner Step 3 timeout khi get_order_detail fail">

Read context from .ai/ and the relevant source files. Then:
1. Produce a step-by-step plan and write it to .ai/current-plan.md
   using the template (Goal / Allowed files / Do not touch / Steps /
   Acceptance criteria / Risks).
2. List the exact files you intend to change.
3. Flag whether this touches sensitive areas (auth/main.py, shared/database.py,
   scanners/main.py, status_sync/*, coordinator/*) -> needs Opus review.
Do NOT write code. Stop after the plan for my approval.
```

→ Bạn đọc `.ai/current-plan.md`, duyệt hoặc chỉnh.

### Bước 2 — IMPLEMENT (mode Code, glm-4.7)

```
Implement the APPROVED plan in .ai/current-plan.md.

Rules:
- Only edit files listed under "Allowed files". Do not touch anything else.
- Follow .ai/coding-standards.md (curl_cffi for marketplace APIs, paramiko
  for deploy, threading.Lock for DB access, etc.).
- After implementing, run the verify commands from .ai/test-commands.md
  (py_compile at minimum, check_all_processes.py if deployed).
- If a test fails, report the exact failure before attempting a fix.
- Keep changes minimal and within scope.
```

### Bước 3 — DEBUG nếu fail (mode Debug, glm-4.7)

```
This failed:
<dán lệnh + output lỗi, vd py_compile traceback hoặc log từ bot server>

Diagnose the root cause first and explain it in 1-2 sentences.
Then fix ONLY the failing issue, within the current scope.
Re-run the failing command to confirm it passes. Do not refactor unrelated code.
```

### Bước 4 — COMMIT

```bash
git add <files trong scope>
git commit -m "<type>: <mô tả ngắn>"
```

Nhờ Ask (glm-4.5-air) viết commit message nếu muốn:
```
Write a concise conventional-commit message for this staged diff:
<dán `git diff --staged --stat` hoặc tóm tắt>
Format: <type>(<scope>): <subject>. One line, under 72 chars.
```

### Bước 5 — GHI LOG

Cập nhật `.ai/task-log.md` (1–2 dòng) và `.ai/decisions.md` nếu có quyết định quan trọng.

### Bước 6 — ĐÓNG SESSION (nếu dài)

```
Summarize this session into .ai/handoff.md using the handoff template:
what is done, what is in-progress (file + line), key decisions, next steps,
and any gotchas. Be concise.
```

→ Mở session mới, bảo Architect/Code đọc `.ai/handoff.md` trước.

---

## 4. Review protocol

### 4.1 GLM Reviewer — first-pass (mọi diff)

Trước khi commit, chạy mode **Reviewer** (glm-5.1, read-only):

```
Review ONLY the staged git diff (run `git diff --staged`). Read-only.
Output:
- Verdict: APPROVE or REJECT
- Blocking issues (bugs/security/regressions) with file:line
- Non-blocking suggestions
- If REJECT, list specific reasons.
Do not edit code. Describe fixes in words; let Code mode implement them.
```

### 4.2 Claude Opus — review cuối (chỉ khi cần)

**Tiêu chí bật Opus** (chỉ cần 1 trong các điều kiện):

- Đụng `auth/main.py`, `shared/database.py` schema, ERP webhook payload
- File trong AGENTS.md "Sensitive areas"
- Diff lớn (nhiều file / vài trăm dòng+)
- Trước merge/deploy
- Bug khó GLM sửa mãi không xong

**BỎ QUA Opus**: sửa lặt vặt, chỉ docs, chỉ test, refactor test xanh.

### 4.3 Cách chạy review Opus

1. Trong Zoo Code: `git add` phần thay đổi
2. Mở **Claude Code** (Pro, bản sạch), trong repo chạy:
   ```
   /ultrareview
   ```
   hoặc:
   ```
   Review the STAGED git diff only. Read-only, do not edit any files.
   Focus on: auth/payment/DB safety, webhook payload correctness,
   ERP state transitions. Check against .ai/coding-standards.md.
   Output: Verdict APPROVE/REJECT + blocking issues with file:line.
   If REJECT: exact reasons and fix described in words (do not write code).
   ```
3. Opus trả APPROVE/REJECT + lý do. **Opus không sửa code.**
4. Nếu REJECT → quay lại Zoo Code **Code** mode:
   ```
   The reviewer rejected the change with these reasons:
   <dán lý do từ Opus>
   Fix only these issues, within scope. Re-run tests. Do not change anything else.
   ```
5. Review lại tới khi APPROVE → merge/deploy.

---

## 5. Quota checklist

- [ ] glm-4.7 là mặc định. glm-5.1 chỉ cho plan/task khó.
- [ ] glm-5.1 chạy **off-peak** (ngoài 13:00–17:00 giờ VN).
- [ ] Một session = một mục tiêu. Dài quá → `handoff.md` + session mới.
- [ ] Gửi 1 prompt đầy đủ context thay vì nhiều prompt nhỏ.
- [ ] Không nhiều agent cùng sửa một file (cần song song → `git worktree`).
- [ ] Reviewer (GLM hoặc Opus) chỉ đọc diff, không tự sửa code.
- [ ] Theo dõi cả quota **tuần** (~2000 prompt/tuần cho Pro), không chỉ 5 giờ.
- [ ] Opus chỉ cho diff lớn/nhạy cảm/pre-merge. ~95% GLM / 5% Opus.
- [ ] Commit nhỏ, test xanh trước khi đi tiếp.

---

## 6. Prompt library — BotPasteDon cụ thể

### Architect — Plan task

```
Task: <mô tả>

Read .ai/{project-context,architecture,coding-standards,test-commands}.md
and the relevant source files. Write a plan to .ai/current-plan.md with:
- Goal (1-2 câu kết quả mong muốn)
- Allowed files (danh sách cụ thể)
- Do not touch (file nhạy cảm ngoài scope)
- Steps (checklist, mỗi bước 1 file cụ thể)
- Acceptance criteria (bằng chứng verify)
- Risks / needs Opus review? (yes/no + lý do)

Do NOT write code. Stop for my approval.
```

### Code — Implement

```
Implement the approved plan in .ai/current-plan.md.
Only edit files under "Allowed files". Follow .ai/coding-standards.md.
After coding, run:
  python -c "import py_compile; py_compile.compile('<file>', doraise=True); print('OK')"
for each changed file. Report any failure exactly.
```

### Debug — Fix failure

```
This failed:
<dán output>

Diagnose root cause in 1-2 sentences. Fix ONLY this issue within scope.
Re-run the failing command to confirm. Don't touch unrelated code.
```

### GLM Reviewer — First-pass

```
Review ONLY the staged git diff (run `git diff --staged`). Read-only.
Output:
- Verdict: APPROVE or REJECT
- Blocking issues with file:line
- Non-blocking suggestions
Do not edit code.
```

### Opus Review (chạy trên Claude Code)

```
Review the staged git diff only. Read-only.
Focus: auth safety, DB schema, webhook payload (total_price/earning/channel_fee),
ERP state transition correctness. Check for missing paramiko patterns,
pkill self-match traps, and threading.Lock on DB access.
Verdict APPROVE/REJECT + blocking issues with file:line.
Do not write code.
```

### Session Handoff

```
Summarize this session into .ai/handoff.md:
- Done (commit hashes)
- In-progress (file + line + what's left)
- Key decisions
- Next steps (ordered)
- Gotchas (things next session must know)
Be concise.
```
