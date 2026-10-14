# 🍽️ Multi-Branch Restaurant AI Agent 🤖

A full-stack, multi-branch restaurant platform with a conversational AI agent for ordering, table reservations, and customer support — built as a zero-cost system running entirely on local infrastructure (self-hosted LLM, no paid APIs).

**Stack:** FastAPI ⚡ · LangGraph 🕸️ · PostgreSQL 🐘 · React ⚛️ · Ollama (DeepSeek R1) 🦙

> 📖 See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the technical deep dive — agent design, database guarantees, and the concurrency/data-integrity decisions behind this project.

---

## 🎯 What this is

Customers order and book tables two ways: chat with an AI agent in natural language, or use the click-through UI — a sidebar cart with a checkout form, and a table-booking form. Both paths run through the same service layer, so an order placed by clicking obeys the same pricing, stock and booking rules as one placed by chatting. Staff manage everything (orders, reservations, stock, branches) from a separate admin dashboard. The system runs across multiple restaurant branches, each with its own menu, inventory, and tables.

The interesting engineering problem this project solves isn't "wire an LLM to a chat box" — it's making an LLM-driven agent safely perform actions that touch real, shared, concurrently-accessed state (inventory, table bookings) without overselling stock or double-booking a table, while running on a small local model that isn't as reliable as a hosted frontier model.

## 🏗️ Why it's built this way

- **💸 Zero ongoing cost.** The LLM (DeepSeek R1, via Ollama) runs locally instead of calling a paid API — a deliberate constraint, not an oversight, made because this was built as a self-funded portfolio project rather than for a client with an infrastructure budget.
- **🛡️ PostgreSQL does the safety-critical work, not the LLM.** An LLM can be persuaded, confused, or just wrong. Two guarantees in this system are enforced at the database level, so they hold even if the agent's reasoning goes off the rails:
  - **🚫 No double-booked tables**, via a PostgreSQL exclusion constraint (`EXCLUDE USING gist`) — the database physically rejects an overlapping reservation, not application code that might have a bug or a race condition.
  - **📦 No overselling stock**, via an atomic conditional `UPDATE ... WHERE stock_quantity >= :qty` — under concurrent orders for the last unit of an item, exactly one succeeds and the other is rejected, guaranteed by the database's own row-level locking rather than a check-then-write race in Python.
- **🧩 Multi-agent graph, not one giant prompt.** The conversational agent is a LangGraph `StateGraph` with a router and separate specialized nodes per task (ordering, reservations, status lookups, FAQ) rather than one LLM call with every tool bound at once — this keeps each domain's prompt and tool set small and independently testable as the system grows.

## ✨ Features

**👤 Customer side**
- 💬 Conversational ordering and table booking via natural language
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
- 🔒 Branch-scoped permissions: a branch-bound staff account only ever sees and acts on its own branch; branch create/edit is admin-only

## 💻 Tech stack

| Layer | Technology |
|---|---|
| 🤖 Conversational agent | LangGraph (multi-node `StateGraph`), Ollama running `deepseek-r1` |
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
- A branch's `opening_hours` is one daily range (`HH:MM - HH:MM`); overnight spans and split shifts are out of scope, and the booking window (15-minute lead, 30-day horizon) is a fixed constant rather than per-branch policy.
- Reservation and order times are stored and shown as the wall clock the guest typed (`19:00` stays `19:00` in every surface, no timezone conversion), which keeps the UI consistent with the chat replies but is not timezone-aware.
- A small local model (`deepseek-r1:8b`/`1.5b`) is meaningfully less reliable at tool-calling than a hosted frontier model; the agent includes defensive parsing around this for exactly that reason.

## 📁 Project layout

```
backend/app/
├── agent/          # 🤖 LangGraph nodes, tools, LLM client, checkpointing
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
