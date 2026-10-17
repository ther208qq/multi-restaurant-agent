# 🍽️ Multi-Branch Restaurant AI Agent 🤖

A full-stack, multi-branch restaurant platform with a bounded LLM diagnosis agent for staff and small customer-help agent helpers — built as a zero-cost system running entirely on local infrastructure (self-hosted LLM, no paid APIs).

**Stack:** FastAPI ⚡ · LangGraph 🕸️ · PostgreSQL 🐘 · React ⚛️ · Ollama (DeepSeek R1) 🦙

> 📖 See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the technical deep dive — agent design, database guarantees, and the concurrency/data-integrity decisions behind this project.

---

## 🎯 What this is

Customers order and book tables through the click-through UI — a sidebar cart with a checkout form, and a table-booking form — and can send natural-language requests to the kitchen about an existing order. Staff manage everything (orders, reservations, stock, branches) from a separate admin dashboard, which also hosts a bounded diagnosis agent that investigates business questions with read-only tools. The system runs across multiple restaurant branches, each with its own menu, inventory, and tables.

The interesting engineering problem this project solves isn't "wire an LLM to a chat box" — it's making an LLM-driven agent safely investigate real, shared, concurrently-accessed state (inventory, table bookings) without overselling stock or double-booking a table, while running on a small local model that isn't as reliable as a hosted frontier model.

## 🏗️ Why it's built this way

- **💸 Zero ongoing cost.** The LLM (DeepSeek R1, via Ollama) runs locally instead of calling a paid API — a deliberate constraint, not an oversight, made because this was built as a self-funded portfolio project rather than for a client with an infrastructure budget.
- **🛡️ PostgreSQL does the safety-critical work, not the LLM.** An LLM can be persuaded, confused, or just wrong. Two guarantees in this system are enforced at the database level, so they hold even if the agent's reasoning goes off the rails:
  - **🚫 No double-booked tables**, via a PostgreSQL exclusion constraint (`EXCLUDE USING gist`) — the database physically rejects an overlapping reservation, not application code that might have a bug or a race condition.
  - **📦 No overselling stock**, via an atomic conditional `UPDATE ... WHERE stock_quantity >= :qty` — under concurrent orders for the last unit of an item, exactly one succeeds and the other is rejected, guaranteed by the database's own row-level locking rather than a check-then-write race in Python.
- **🧩 Two agent surfaces, each bounded.** The staff diagnosis agent is a LangChain `create_agent` loop restricted to six read-only tools and a fixed step budget. Customer-facing help (KDS messages, gift suggestions, booking offers) is handled by small, purpose-built helpers over the Services. There is no standalone customer chat endpoint: the unwired legacy multi-node `StateGraph` has been removed.

## ✨ Features

**👤 Customer side**
- 💬 Natural-language "contact the kitchen" requests on an order (hold / resume / status), classified by the customer agent helper
- 🛒 Self-service ordering without the agent: click-to-add cart with variant selection, a live cost preview, and a checkout form (dine-in / takeaway / delivery)
- 📅 Self-service table booking form with live per-slot availability and automatic table assignment
- 📜 Live menu browsing with real-time per-branch stock and sold-out states
- ✅ Table reservations with a staff-confirmed lifecycle (`pending → confirmed → seated → completed`) and a real double-booking guard
- 🔐 Account system (JWT-based) with order/reservation history
- 🏢 Multi-branch selection with branch-specific hours, address, and policies

**👨‍🍳 Staff side**
- 📋 Live order board with status workflow (`placed → preparing → ready → delivered`, plus cancel/un-cancel with correct stock reversal)
- 📅 Reservation management: availability lookup, confirm / seat / complete / cancel, reschedule and manual table reassignment
- ✏️ Menu and per-branch stock editor
- ⚙️ Branch and table configuration
- 🔍 Operation audit log: every key action (order cancellation, stock adjustment, table reassignment, reservation change, menu/branch edits) recorded with who did it and the before/after values
- 📊 Business statistics: order volume, revenue, average order value, units sold, a best-selling-dish ranking and a daily breakdown for any branch and date range — cancelled orders excluded from the totals and reported separately
- 🕵️ Agentic business diagnosis (admin dashboard): ask "最近生意为什么下降？" and a bounded LangGraph ReAct agent picks the time window itself, chooses read-only investigation tools (deterministic diagnosis, daily series, per-dish detail, peer comparison, audited operational changes), and answers with citable evidence refs — separating observed facts, evidence-backed inferences, and explicitly unverified hypotheses. Limited to 6 tool calls and 24 graph steps per turn; if the provider is unavailable it degrades to the deterministic diagnosis. Follow-ups ("那周末呢？", "和别的门店比呢？") continue the same thread
- 🔒 Branch-scoped permissions: a branch-bound staff account only ever sees and acts on its own branch; branch create/edit is admin-only

## 💻 Tech stack

| Layer | Technology |
|---|---|
| 🤖 Customer agent helpers | `agent/kds.py`, `agent/approvals.py`, `agent/offers.py` — LLM/rule classification over Service calls |
| 🕵️ Diagnosis agent | LangChain `create_agent` (bounded ReAct loop, 6 tool calls / 24 steps), LangChain chat model over Ollama or any OpenAI-compatible endpoint |
| ⚡ Backend API | FastAPI, SQLAlchemy (async), Pydantic |
| 🐘 Database | PostgreSQL 16 (`btree_gist` extension for exclusion constraints) |
| 🔑 Auth | JWT (customer + staff, separate roles) |
| 🎨 Frontend | React + Vite |
| 💾 Persistence for agent memory | `AsyncPostgresSaver` (LangGraph checkpointing) — conversations survive across sessions |

## 🚀 Running it locally

**Prerequisites:** Python 3.11 or 3.12, Node.js 18+, Docker (for Postgres) or a local PostgreSQL 16 install, [Ollama](https://ollama.com).

```bash
# 1. 🗄️ Database
docker compose up -d

# 2. 🐍 Backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
cp .env.example .env
python -m backend.app.seed
uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000

# 3. 🧠 Local LLM (separate terminal)
ollama pull deepseek-r1:8b          # or deepseek-r1:1.5b on lower-memory machines
ollama run deepseek-r1:8b

# 4. ⚛️ Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

🌐 App runs at `http://localhost:5173`. API docs (Swagger) at `http://localhost:8000/docs`.

**🔑 Seeded accounts** (change before deploying anywhere public):
| Role | Login |
|---|---|
| 👑 Superadmin | `admin` / `admin123` |
| 🏪 Branch staff | `downtown_staff` / `staff123` |
| 🛒 Sample customer | `customer@example.com` / `customer123` |

**🔬 Evaluation dataset (optional, isolated).** `scripts/import_public_sales_dataset.py --create-database` imports the public Maven coffee-shop sales dataset into a separate `restaurant_ai_eval` database and creates the evaluation accounts `eval_admin` / `admin123` and `eval_staff` / `staff123`. Start the API against that database with `python backend/run_eval.py` (the development database and its accounts stay untouched); `python backend/run.py` keeps using the development database. The login dialog asks `GET /api/staff/login-hints` which account set is live, so it never advertises accounts that the running backend cannot authenticate.

## 🧪 Testing

```bash
pytest -v
```

Covers: JWT auth flows, branch-scoped staff permissions, the operation audit trail (including that a rejected operation leaves no audit row), the statistics aggregates (cancelled orders excluded, UTC day boundaries, branch isolation), duplicate-request suppression for both orders and bookings, atomic stock decrement under concurrency, reservation lifecycle transitions and rescheduling, reservation time-window enforcement (lead time, 30-day horizon, opening-hours fitting, concrete near-slot suggestions), the PostgreSQL exclusion constraint rejecting overlapping reservations in any occupying status, and order cancel/un-cancel stock reversal.

The statistics and duplicate-request tests are also the failure-path suite: a short second line in a multi-line order, a booking that loses the table to someone else, and an illegal status transition each assert that **nothing** was written — no row, no stock movement, no audit entry.

## 🚧 Known limitations

Documented deliberately rather than discovered by a reader — this is a portfolio/demo system, not a production deployment:
- Local LLM + local Postgres means this isn't deployed anywhere persistent by default; it's designed to run on one machine.
- No payment processing — orders are marked `placed` and handled manually by staff, by design.
- Duplicate-request suppression is a five-minute window scoped to still-open requests, not a general idempotency-key system: a genuinely intentional repeat of the identical order a few minutes later goes through, and a *different* request in the same window always does.
- Statistics are aggregated per UTC day (the database stores `timestamptz`); a branch in another timezone would need a per-branch offset to have its "day" line up with the local one.
- A branch's `opening_hours` is one daily range (`HH:MM - HH:MM`); overnight spans and split shifts are out of scope, and the booking window (more than 15 minutes ahead, 30-day horizon) is a fixed constant rather than per-branch policy. The window's "now" is the server's local wall clock, to match the wall-clock times the guest types — so it assumes the server runs in the branch's timezone.
- Reservation and order times are stored and shown as the wall clock the guest typed (`19:00` stays `19:00` in every surface, no timezone conversion), which keeps the UI consistent with the server's booking rules but is not timezone-aware.
- No standalone customer chat endpoint: customer actions run through the REST routes, and the only natural-language customer surface is the "contact the kitchen" request. The legacy multi-node chat graph was removed rather than left unwired.
- A small local model (`deepseek-r1:8b`/`1.5b`) is meaningfully less reliable at tool-calling than a hosted frontier model; the agent includes defensive parsing around this for exactly that reason.

## 📁 Project layout

```
backend/app/
├── agent/          # 🤖 Diagnosis investigator, KDS/approval/offer helpers, LLM client, checkpointing
├── models/         # 🗃️ SQLAlchemy models (branches, menu, orders, reservations, users, audit)
├── repo/           # 🗄️ Data access (only layer that touches the DB)
├── service/        # ⚙️ Business rules, transactions, permissions, audit recording
├── routes/         # 🛣️ FastAPI routers (customer + staff APIs)
├── schemas/        # 📄 Pydantic request/response schemas
└── seed.py         # 🌱 Demo data seeding

frontend/src/
├── components/customer/   # 👤 Chat widget, menu explorer + cart, checkout & booking forms, auth, history
└── components/admin/      # 👔 Staff dashboard (orders, reservations, menu, branches)
```
