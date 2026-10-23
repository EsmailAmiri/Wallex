# Mandatory project rules

Read and follow `C:\Users\Esmail\.codex\RTK.md` for shell commands.

This project is constrained by the user's **هکاتون استارکوچ | والکس** credit plan. These are core requirements, not optional UI preferences. Do not remove, relax, bypass, or hide them when adding strategies, integrations or models.

- Source of truth: `challenge_rules.py`, `rules/blocked_markets.txt`, and `docs/CHALLENGE_RULES.md`.
- Personal capital: **0 TMN**; credit and performance baseline: **5,000,000 TMN**, fixed.
- Exact activation: **1405/07/29 20:32 Asia/Tehran** = **2026-10-21T20:32:00+03:30**.
- Exact expiry: **1405/08/02 20:32 Asia/Tehran** = **2026-10-24T20:32:00+03:30**; maximum 72 hours. Never calculate a new deadline from process start, model changes, reset, reconnect or reinstall.
- Credit fee for this specific user-provided plan: **0 TMN**. Exchange transaction fees are separate and start at account level one; do not invent a verified percentage.
- Every pair in `rules/blocked_markets.txt` is prohibited. Enforce canonical normalization and rejection in backend, engine and MCP. Absence from this denylist does not prove current exchange availability.
- Prohibited account capabilities: margin/leverage, native Wallex trading bots, deposits, withdrawals and transfers between accounts. Do not add automatic plan activation, termination, renewal or funding.
- Advanced spot trading, instant trading and API management are allowed by the account plan. The user explicitly authorizes the project's trading software to execute real spot trades autonomously, including selecting permitted markets, sizing orders within the credit plan and configured exposure limits, buying, selling, tracking and cancelling orders, and reconciling fills. No per-order user confirmation is required for this software within those bounds. Development of these capabilities is within project scope. This authorization does not establish that live trading is implemented, enabled, tested or permitted by the exchange key. Keep paper/shadow and real account performance clearly separate.
- Use the dedicated credit-account API key. A successful generic balance query does not prove credit-account identity, basic KYC, enrollment eligibility, or liquidity status. Do not claim these were verified unless independently established.
- Expiry stops the shadow agent and may close the simulated ledger using the last observed price; only Wallex performs the real account liquidation/settlement. Never label a simulated close as a real fill.
- Preserve `.env`; never print, commit, expose, or include its secret values in browser data, exports, screenshots or logs.
- Active integration is the official Streamable HTTP MCP endpoint `https://api.wallex.ir/mcp`, verified against `https://wallex.ir/landing/mcp/#steps`. Use `X-API-Key` only for this exact endpoint and disable redirects. The project's trading software may use explicitly allowlisted read tools and permitted spot order creation, status and cancellation tools through a validated live-order adapter. The existing read-only dashboard guard must remain effective until that adapter and its policy checks are implemented and tested; do not bypass it or expose arbitrary MCP tool execution. Discovering a tool does not grant access to capabilities prohibited by the credit plan. Preserve the hackathon guards around official responses.
- Keep the supplied inactive-market list unchanged unless the user explicitly supplies an update; changes must update policy version and relevant tests.
- User submission cutoff is separately fixed in `competition.py`: Friday 2026-10-23 23:50 Asia/Tehran. Shadow execution stops at the earlier cutoff; do not change the credit-plan expiry to match it.
- LLM uses a separate 9router key. It may veto candidate entries only; forced exits and policy checks must not wait for it. No account secrets/balances in prompts. Invalid replies/timeouts veto entry. Preserve request/model/prompt hashes and immutable vote cache for evaluation; replay must never make new model calls.
- `ta_trend` uses the MIT-licensed `ta==0.11.0` library for EMA, ADX/DI, RSI and ATR. Preserve closed-bar-only inputs, next-open hypothetical fills, exact fee/slippage accounting, and the frozen-data development/holdout boundary documented in `docs/RESEARCH_1405-07-30.md`.
- Test rejected symbols, attempted principal/time overrides, deadline boundaries, reset/restart behavior, and final TMN settlement when modifying this policy or execution paths.

## Authorized autonomous trading software

- The user has granted authorization for autonomous live spot trading by the project's software. Do not repeatedly request per-order approval. Authorization alone must not switch the current runtime to live mode: first implement and test the adapter, verify dedicated credit-account scope and validate live configuration. Generic balances or the presence of a sub-account record are insufficient verification. Keep the current paper/shadow default until those prerequisites are satisfied.
- Before an order, enforce the fixed credit plan, applicable deadlines, canonical market denylist, current spot availability, exchange quantity/notional/precision requirements, available funds and configured exposure limits. Do not silently increase the test budget to meet an exchange minimum.
- Use a unique client order ID and persist the intended order before submission. After a timeout or uncertain response, reconcile by that ID before any retry; do not submit duplicate orders blindly.
- Record exchange-confirmed order status, executed quantity, fills and fees separately from simulated records. An accepted order is not a confirmed fill, and a failed read is not an order rejection.
- Verify the adapter using mocks or a documented non-financial test facility before user-run deployment. Never use invalid live orders as a substitute for permission checks. This section governs the project's deployed trading software; editing this file does not change the coding assistant's own tool-use limits.
