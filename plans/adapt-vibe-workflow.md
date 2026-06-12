# Plan: Adapt BotPasteDon to Vibe Coding Workflow

**Ngày:** 2026-06-12
**Loại:** Chore — quy trình, không đụng logic code
**Tình huống:** Repo đang chạy (TH2) — thêm quy trình vận hành, không đổi behavior

---

## 1. Gap Analysis — Hiện tại vs Workflow yêu cầu

### Đã có (không cần đổi)

| Thành phần | File | Đánh giá |
|---|---|---|
| Agent rules | `AGENTS.md` | ✅ Đầy đủ commands, rules, out-of-scope, sensitive areas |
| Project context | `.ai/project-context.md` | ✅ Chi tiết 83 dòng, stack + topology + constraints |
| Architecture | `.ai/architecture.md` + `docs/architecture.md` | ✅ 87 dòng tóm tắt + 333 dòng đầy đủ |
| Coding standards | `.ai/coding-standards.md` | ✅ 148 dòng, pattern cụ thể |
| Test commands | `.ai/test-commands.md` | ✅ 107 dòng, project-specific |
| Plan template | `.ai/current-plan.md` | ✅ Template đúng format Goal/Allowed/Steps/Risks |
| Task log | `.ai/task-log.md` | ✅ Active, entries 2026-06-05 → 2026-06-10 |
| Decisions log | `.ai/decisions.md` | ✅ 326 dòng ADR |
| Handoff | `.ai/handoff.md` | ✅ 136 dòng, state cuối session |

### Cần thêm / cập nhật

| # | Thành phần | Vấn đề | Hành động |
|---|---|---|---|
| G1 | **Model hierarchy** | AGENTS.md không ghi model nào cho role nào | Thêm section "Model Assignment" |
| G2 | **Review protocol** | Chỉ ghi "Opus review" chung chung, không có criteria rõ | Thêm escalation matrix |
| G3 | **Off-peak timing** | Không nhắc giờ cao điểm GLM | Thêm rule off-peak |
| G4 | **Daily task cycle** | Không mô tả vòng lặp Plan→Code→Debug→Commit→Log | Thêm section "Daily Task Cycle" |
| G5 | **Quota discipline** | Không có rule quota | Thêm checklist ngắn |
| G6 | **Commit sign-off** | Ghi "Claude Opus 4.7" — nhầm lẫn model | Sửa thành format chuẩn |
| G7 | **Workflow reference** | Prompt templates nằm ngoài repo | Tạo `.ai/workflow-reference.md` |
| G8 | **Custom mode configs** | Chưa có `.roo/` setup cho model pinning | Tạo mode configs |

---

## 2. Allowed files

```
AGENTS.md                           — Cập nhật rules + thêm sections mới
.ai/coding-standards.md             — Thêm model + review guidelines
.ai/project-context.md              — Thêm methodology reference
.ai/workflow-reference.md           — FILE MỚI: condensed daily workflow
.roo/                               — FILE MỚI: custom mode configs
```

## 3. Do not touch

```
Any .py source code
docs/architecture.md, docs/operations.md, docs/marketplace_auth.md
.ai/current-plan.md, .ai/task-log.md, .ai/decisions.md, .ai/handoff.md
.env, .env.example, data/
```

## 4. Steps

### B1. Cập nhật `AGENTS.md`

Thêm 4 section mới vào sau phần "Rules" hiện tại, trước "Out of scope":

**a) Section "Model Assignment"**
```
## Model Assignment

| Vai | Model | Dùng khi |
|---|---|---|
| Architect / Plan | glm-5.1 | off-peak ONLY — ngoài 13:00-17:00 VN |
| Code / Implement | glm-4.7 (mặc định) → glm-5-turbo khi khó | Luôn |
| Debug | glm-4.7 → glm-5-turbo nếu fail 2 lần | Khi test fail |
| Docs / Commit msg | glm-4.5-air | Task nhỏ, markdown |
| Review first-pass | glm-5.1 (Reviewer mode) | Diff vừa, không nhạy cảm |
| Review cuối | Claude Opus qua Claude Code | Diff lớn / auth / payment / DB |

Off-peak = ngoài 13:00-17:00 giờ VN (14:00-18:00 UTC+8).
Giờ cao điểm GLM-5.1 tốn 3x quota.
```

**b) Section "Daily Task Cycle"**
```
## Daily Task Cycle

Một session = một mục tiêu. Vòng lặp:

1. PLAN (Architect / glm-5.1, off-peak)
   → Đọc context từ .ai/ + source files liên quan
   → Ghi plan vào .ai/current-plan.md (Goal / Allowed / Do-not-touch / Steps / Acceptance / Risks)
   → Dừng chờ duyệt

2. IMPLEMENT (Code / glm-4.7)
   → Chỉ sửa file trong "Allowed files"
   → Theo .ai/coding-standards.md
   → Chạy test/typecheck, báo lỗi chính xác nếu fail

3. DEBUG nếu fail (Debug / glm-4.7)
   → Chẩn đoán root cause 1-2 câu
   → Fix CHỈ lỗi đó, trong scope
   → Re-run test

4. COMMIT
   → git add <files trong scope>
   → Commit message conventional-commit, subject ≤72 chars
   → Sign-off: Co-Authored-By: Agent <model> khi agent làm bulk

5. LOG
   → Append 1-2 dòng vào .ai/task-log.md
   → Ghi .ai/decisions.md nếu có quyết định quan trọng

6. ĐÓNG SESSION (nếu dài)
   → Tóm tắt vào .ai/handoff.md
   → Session mới đọc handoff trước khi bắt
```

**c) Section "Review Protocol"**
```
## Review Protocol

### Khi nào GLM Reviewer (first-pass)
Mọi diff trước khi commit. Mode Reviewer (glm-5.1), read-only.
Chỉ chạy `git diff --staged`, báo APPROVE/REJECT + lý do.

### Khi nào Claude Opus (review cuối)
CHỈ khi diff thỏa MỘT trong các điều kiện:
- Đụng auth, payment, database schema, migration
- Đổi hành vi webhook payload (ERP money fields)
- File trong danh sách "Sensitive areas" dưới đây
- Diff lớn (nhiều file / vài trăm dòng+)
- Trước merge/deploy
- Bug khó GLM sửa mãi không xong

BỎ QUA Opus: sửa lặt vặt, chỉ docs, chỉ test, refactor test xanh.

Tỷ lệ mục tiêu: ~95% GLM / 5% Opus.

### Cách chạy review Opus
1. Trong Roo: `git add` thay đổi
2. Mở Claude Code (Pro), chạy: /ultrareview
3. Opus báo APPROVE/REJECT + lý do — KHÔNG sửa code
4. Nếu REJECT → về Roo Code mode, dán lý do, GLM sửa
```

**d) Cập nhật sign-off format**
```
# Hiện tại:
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
# Đổi thành (linh hoạt theo model thực tế):
Co-Authored-By: GLM-4.7 <noreply@zhipuai.cn>
# Hoặc nếu là Opus review-driven:
Co-Authored-By: Claude Opus 4 <noreply@anthropic.com>
```

### B2. Cập nhật `.ai/coding-standards.md`

Thêm vào cuối file, trước phần "Anti-patterns to avoid":

```markdown
## Model usage guidelines

- **glm-4.7 là mặc định** cho mọi việc code, debug, refactor.
- **glm-5.1 chỉ dùng cho**: plan, task dài, code khó. Chạy off-peak.
- **glm-5-turbo**: nâng từ glm-4.7 khi bug phức tạp hoặc code khó.
- **glm-4.5-air**: docs, commit messages, task nhỏ, scout.
- **Claude Opus**: review only, KHÔNG code. Dùng `/ultrareview` trên Claude Code Pro.

## Review escalation rules

1. Mọi commit → GLM Reviewer first-pass (mode Reviewer, glm-5.1).
2. Nếu diff chạm file trong AGENTS.md "Sensitive areas" → BUỘC Claude Opus review.
3. Nếu diff > 200 dòng đổi behavior → khuyến nghị Opus review.
4. Refactor/docs/test chỉ → GLM Reviewer, bỏ qua Opus.
```

### B3. Cập nhật `.ai/project-context.md`

Thêm vào cuối file một section:

```markdown
## Vibe Coding Methodology

Dự án vận hành theo workflow "Vibe Coding" (GLM Pro + Claude Pro):
- **Quy trình chuẩn:** Plan (glm-5.1, off-peak) → Code (glm-4.7) → Debug → Commit → Log
- **Review:** GLM Reviewer first-pass, Claude Opus cho diff lớn/nhạy cảm
- **Thông tin chảy qua `.ai/` files** — không qua môi riêng giữa modes
- **Chi tiết đầy đủ:** `.ai/workflow-reference.md`
- **Nguồn methodology:** `D:\AI vibe coding\{DAILY-WORKFLOW,WORKED-EXAMPLES,ke-hoach-vibe-coding-2026}.md`
```

### B4. Tạo `.ai/workflow-reference.md` (FILE MỚI)

File này là bản rút gọn, project-specific của `DAILY-WORKFLOW.md` + `WORKED-EXAMPLES.md`.

Nội dung chính:
1. **Bản đồ tool/model** — bảng tóm tắt (như DAILY-WORKFLOW section 0)
2. **Cấu trúc file** — nhắc nhanh cấu trúc `.ai/` (đã có, chỉ liệt kê)
3. **Vòng lặp task** — 6 bước với prompt templates CHỈNH CHO PROJECT NÀY
4. **Review protocol** — khi nào GLM vs Opus (CHỈNH: thêm sensitive areas cụ thể của BotPasteDon)
5. **Quota checklist** — 8 điều kỷ luật
6. **Prompt library** — sẵn sàng copy-paste cho BotPasteDon:
   - Prompt Architect plan task
   - Prompt Code implement
   - Prompt Debug fix
   - Prompt GLM Reviewer
   - Prompt Opus review (dán sang Claude Code)
   - Prompt session handoff

### B5. Tạo `.roo/` custom mode configs

Tạo cấu trúc:
```
.roo/
├── rules/
│   ├── architect.md      — Architect mode rules (ghi đè default)
│   ├── code.md           — Code mode rules
│   └── reviewer.md       — Reviewer mode rules
```

Nội dung mỗi file:
- **architect.md**: Chỉ ghi markdown, KHÔNG code. Luôn đọc `.ai/` context trước. Dừng chờ duyệt plan.
- **code.md**: Chỉ sửa file trong current-plan "Allowed files". Chạy test sau khi sửa. Báo lỗi chính xác.
- **reviewer.md**: Read-only. Chỉ đọc `git diff --staged`. Output APPROVE/REJECT + lý do.

Lưu ý: Model pinning (glm-5.1 cho Architect, glm-4.7 cho Code) được cấu hình qua UI settings của Roo Code, không qua file `.roo/`. File `.roo/rules/` chỉ chứa rules text.

## 5. Acceptance Criteria

- [ ] `AGENTS.md` có 4 section mới: Model Assignment, Daily Task Cycle, Review Protocol, sign-off cập nhật
- [ ] `.ai/coding-standards.md` có thêm "Model usage guidelines" + "Review escalation rules"
- [ ] `.ai/project-context.md` có section "Vibe Coding Methodology"
- [ ] `.ai/workflow-reference.md` tồn tại, có đủ 6 mục, prompt templates đã chỉnh cho BotPasteDon
- [ ] `.roo/rules/` có 3 file rules cho architect, code, reviewer modes
- [ ] Không file .py nào bị sửa
- [ ] Syntax/convention nhất quán với style hiện có

## 6. Risks

- **Risk thấp** — chỉ thay đổi docs/config, không đụng code
- **Không cần Opus review** — diff toàn markdown, không logic change
- **Model pinning** cần cấu hình thủ công qua Roo Code UI → ghi hướng dẫn vào workflow-reference.md
