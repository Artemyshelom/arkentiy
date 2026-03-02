"""
app/ctx.py — ContextVar для мульти-тенантного polling loop.

Каждый polling loop (или per-message resolver) устанавливает свой контекст —
все хелперы используют их автоматически без протаскивания через аргументы.
"""
from contextvars import ContextVar

ctx_tenant_id: ContextVar[int] = ContextVar("tenant_id", default=1)
ctx_bot_token: ContextVar[str] = ContextVar("bot_token", default="")
