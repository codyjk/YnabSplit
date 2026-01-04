# Splitwise → YNAB Clearing Transaction Automator (API-first, Python + uv)

## Problem statement

My wife and I each keep separate YNAB budgets. Shared expenses are tracked in Splitwise and settled periodically via Venmo. The settlement shows up in YNAB as a **single** Venmo/bank transaction, but it represents many underlying Splitwise items that should be categorized across multiple YNAB categories.

**Goal:** Automate creation of an adjacent **clearing transaction** in YNAB that:
- has the same total amount/date/payee characteristics as the Venmo settlement (so bank import will match it later)
- is a **split** transaction where **one Splitwise expense item = one split line** in YNAB
- correctly allocates category inflows/outflows per Splitwise item, while netting to the settlement total

This project is implemented as an idiomatic Python project using `uv` for package management. It is **API-first**.

---

## Key requirements / constraints

- **Strict mode:** 1 Splitwise item → 1 YNAB split line (no grouping/aggregation).
- **Clearing transaction pattern:** Create a new manual YNAB transaction; later the imported bank/Venmo transaction matches it automatically.
- ~100 total YNAB categories across ~10 groups; ~30–40 active (most hidden). Default mapping should use active categories only.
- Category mapping should be mostly deterministic:
  - rules + cache first
  - GPT fallback only for unknown/ambiguous items
- Idempotent: repeated runs should not create duplicate clearing transactions for the same settlement.

---

## High-level architecture

### Components
1. **HTTP API service (primary)**
   - Handles reconciliation workflow
   - Owns adapters for YNAB + Splitwise
   - Owns mapping logic + state store (SQLite)

2. **CLI client (thin wrapper)**
   - Calls HTTP endpoints
   - Useful for local workflows, scripting, Cron

3. **State store (SQLite)**
   - Settlement idempotency + applied transaction ids
   - Merchant/keyword mapping cache and overrides
   - Optional YNAB metadata cache (categories/accounts snapshot)

---

## Core workflow (per settlement)

1. **Identify settlement scope**
   - Input options:
     - Splitwise “settle up” id (preferred if available)
     - OR CSV import id + date range
     - OR explicit list of Splitwise expense ids
   - Include `expected_net_amount` (the Venmo settlement amount) unless derived from settle-up metadata.

2. **Collect Splitwise expenses in scope**
   - Normalize each expense to a single internal model.

3. **Compute signed amounts (your POV)**
   - For each Splitwise expense, compute **your net**:
     - `net = (your_paid_share - your_owed_share)`
   - Interpretation:
     - `net > 0`: you are owed money for this expense → **YNAB inflow** to the mapped category
     - `net < 0`: you owe money for this expense → **YNAB outflow** to the mapped category
   - Convert dollars to **milliunits**.

4. **Map each Splitwise expense to a YNAB category**
   - **Pass 1: hard rules**
     - merchant regex (e.g., “Whole Foods” → Groceries)
     - keyword rules in description/notes
     - optional Splitwise category → YNAB category mapping table
   - **Pass 2: learned mapping cache**
     - `(normalized_merchant | normalized_description_prefix) -> ynab_category_id`
   - **Pass 3: GPT fallback**
     - Only for expenses not mapped by rules/cache
     - Provide only **active categories** by default (30–40)
     - Optionally narrow to top-k candidates via fuzzy match
     - Require structured JSON output: `{category_id, confidence, rationale_short}`
   - If confidence < threshold, mark item `needs_review`.

5. **Validate totals**
   - Sum of all expense nets must equal `expected_net_amount` (milliunits), within tolerance.
   - If off by rounding, adjust the last split line by the residual milliunit(s).
   - If materially off, refuse to apply.

6. **Create a clearing transaction draft**
   - Contains:
     - payee (e.g., “Venmo”)
     - date
     - account (clearing account)
     - total amount (expected net)
     - strict list of split lines (one per Splitwise expense)
     - metadata for audit + idempotency

7. **Optional review/resolve**
   - If any `needs_review`, user provides overrides (expense id → category id).
   - Persist confirmed mappings into cache.

8. **Apply**
   - Create a new YNAB transaction in the clearing account with split lines.
   - Stamp memo with settlement metadata so it’s easy to trace.
   - Record `(settlement_id, draft_hash) -> ynab_transaction_id` in SQLite for idempotency.

---

## Domain model (conceptual)

### SplitwiseExpense
- `id`, `group_id`, `date`
- `description`, `notes`, `merchant` (if derivable)
- `cost`, `currency`
- `paid_by`, `shares` (enough to compute your net)
- `participants`

### Settlement
- `id` (Splitwise settle-up id preferred; otherwise synthetic)
- `date_range` or explicit expense ids
- `expected_net_amount_milliunits`
- `currency`

### ProposedLine (strict: one per expense)
- `splitwise_expense_id`
- `amount_milliunits` (signed)
- `ynab_category_id`
- `memo` (include Splitwise expense id + original description)

### ClearingTransactionDraft
- `draft_id`
- `account_id` (clearing account)
- `payee` / `payee_id`
- `date`
- `total_amount_milliunits`
- `lines: list[ProposedLine]` (1:1 with expenses)
- `metadata`:
  - `settlement_id`, `group_id`
  - `expense_ids`
  - `hash(payload)` (idempotency key)

---

## State store (SQLite) responsibilities

- **processed_settlements**
  - `settlement_id` (unique)
  - `draft_hash`
  - `ynab_transaction_id`
  - timestamps

- **merchant_category_map**
  - `pattern` / `normalized_key`
  - `ynab_category_id`
  - `source` (rule/manual/gpt)
  - `confidence`, timestamps

- **expense_overrides**
  - `splitwise_expense_id` -> `ynab_category_id`

- **ynab_cache (optional)**
  - categories snapshot + refreshed_at
  - accounts snapshot + refreshed_at

---

## Category mapping behavior (cost + reliability)

### Default category universe
- Use **active categories only** (non-hidden) for mapping and GPT candidate lists.
- Allow explicit override to include hidden categories.

### GPT fallback (only when needed)
- Inputs:
  - expense: `{description, notes, merchant, amount, splitwise_category?}`
  - candidate categories: `[{id, group, name}]` (active only; optionally top-k)
- Output (structured):
  - `{category_id: string, confidence: float 0..1, rationale_short: string}`
- Policy:
  - if `confidence >= threshold`: accept
  - else: `needs_review`

### Learning loop
- When user resolves ambiguous items, persist those mappings so next time they’re rule/cached and GPT is unnecessary.

---

## Clearing transaction behavior in YNAB

### The created clearing transaction
- `account_id = clearing_account_id`
- `payee = clearing_payee` (e.g., “Venmo”)
- `date = settlement date` (or user-provided)
- `amount = expected_net_amount` (milliunits)
- `memo` includes:
  - `sw_settlement_id`, `sw_group_id`
  - list/hash of expense ids
  - `draft_hash`

### Split lines (strict)
For each Splitwise expense (exactly one line):
- `category_id = mapped category`
- `amount = computed signed net (milliunits)`
- `memo = "Splitwise: <description> (expense_id=<id>)"`

### Matching expectation
When the real Venmo/bank transaction imports:
- YNAB should match it to the manual clearing transaction based on:
  - same account
  - same amount
  - close date
  - payee/memo similarity (depending on your import source)

---

## HTTP API design (high-level)

### Health / config
- `GET /health`
- `GET /config`
- `PUT /config`
  - store:
    - `ynab_budget_id`
    - `ynab_clearing_account_id`
    - `clearing_payee`
    - `active_categories_only=true`
    - `gpt_threshold`
    - `match_date_tolerance_days`

### YNAB metadata
- `POST /ynab/sync`
  - refresh accounts + categories
- `GET /ynab/categories?active_only=true`
- `GET /ynab/accounts`

### Splitwise ingestion
- `POST /splitwise/import/csv`
  - upload CSV + metadata (group/user context)
  - returns `import_id`, detected date range, expense count
- `GET /splitwise/expenses?import_id=...`
- (later) `POST /splitwise/sync` (API mode)

### Settlement workflow
- `POST /settlements/draft`
  - request supports:
    - `splitwise_settlement_id`
    - OR `import_id + date_range`
    - OR `explicit_expense_ids`
    - `expected_net_amount`
    - `settlement_date`
  - response:
    - `draft_id`
    - draft summary + strict split lines
    - list of `needs_review` items with candidate categories

- `POST /settlements/draft/{draft_id}/resolve`
  - body: `{overrides: [{splitwise_expense_id, ynab_category_id}]}`
  - response: updated draft

- `POST /settlements/draft/{draft_id}/approve`
  - locks draft hash for apply (optional but useful)

- `POST /settlements/draft/{draft_id}/apply`
  - creates clearing transaction in YNAB
  - idempotent:
    - if `(settlement_id, draft_hash)` already applied → return existing `ynab_transaction_id`

### Mapping management
- `GET /mappings`
- `POST /mappings`
  - add/update a rule: `pattern -> ynab_category_id`
- `DELETE /mappings/{id}`
- `POST /mappings/learn`
  - persist confirmed mappings from a draft apply/review

---

## CLI (thin wrapper over HTTP)

- `sync` → `/ynab/sync`
- `import-csv` → `/splitwise/import/csv`
- `draft-settlement` → `/settlements/draft`
- `resolve` → `/settlements/draft/{id}/resolve`
- `apply` → `/settlements/draft/{id}/apply`
- `status` → list recent settlements and their applied YNAB tx ids

---

## Incremental delivery plan

### Phase 1: Draft-only (no YNAB writes)
- CSV import → draft settlement → returns strict split lines + needs_review list
- Validate sign conventions and total net math

### Phase 2: Apply clearing transaction
- Implement `/apply` to create YNAB clearing transaction
- Add memo stamping + idempotency DB record

### Phase 3: GPT fallback + review loop
- Add GPT categorizer only for unmapped items
- Add `needs_review` and `/resolve`
- Persist confirmed mappings for future automation

### Phase 4: Splitwise API mode
- Add Splitwise API ingestion
- Keep CSV import as a permanent fallback

---

## Safety / correctness guarantees (must-have)

- **Dry-run by default:** `/draft` computes everything without writing.
- **Idempotent apply:** never create duplicates for the same settlement+hash.
- **Strict traceability:** every split line references Splitwise expense id in memo.
- **Active-category allowlist:** default mapping among active categories only.
- **Fail closed on mismatch:** refuse to apply if totals don’t reconcile.
- **Rounding control:** adjust residual milliunits on the final line only.
