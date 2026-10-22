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
- Advanced spot trading, instant trading and API management are allowed by the account plan, but real trading is not implemented in this version. Keep paper/shadow and real account performance clearly separate.
- Use the dedicated credit-account API key. A successful generic balance query does not prove credit-account identity, basic KYC, enrollment eligibility, or liquidity status. Do not claim these were verified unless independently established.
- Expiry stops the shadow agent and may close the simulated ledger using the last observed price; only Wallex performs the real account liquidation/settlement. Never label a simulated close as a real fill.
- Preserve `.env`; never print, commit, expose, or include its secret values in browser data, exports, screenshots or logs.
- Active integration is the official Streamable HTTP MCP endpoint `https://api.wallex.ir/mcp`, verified against `https://wallex.ir/landing/mcp/#steps`. Use `X-API-Key` only for this exact endpoint and disable redirects. Only explicitly allowlisted read tools may run; discovering a financial tool does not authorize calling it. Preserve the hackathon guards around official responses.
- Keep the supplied inactive-market list unchanged unless the user explicitly supplies an update; changes must update policy version and relevant tests.
- User submission cutoff is separately fixed in `competition.py`: Friday 2026-10-23 23:50 Asia/Tehran. Shadow execution stops at the earlier cutoff; do not change the credit-plan expiry to match it.
- LLM uses a separate 9router key. It may veto candidate entries only; forced exits and policy checks must not wait for it. No account secrets/balances in prompts. Invalid replies/timeouts veto entry. Preserve request/model/prompt hashes and immutable vote cache for evaluation; replay must never make new model calls.
- `ta_trend` uses the MIT-licensed `ta==0.11.0` library for EMA, ADX/DI, RSI and ATR. Preserve closed-bar-only inputs, next-open hypothetical fills, exact fee/slippage accounting, and the frozen-data development/holdout boundary documented in `docs/RESEARCH_1405-07-30.md`.
- Test rejected symbols, attempted principal/time overrides, deadline boundaries, reset/restart behavior, and final TMN settlement when modifying this policy or execution paths.
