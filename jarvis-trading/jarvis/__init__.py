"""Jarvis Core - the general-purpose, voice-first assistant layer (JARVIS PRIME spec).

Jarvis is the conversational intelligence. The trading engine stays the source of truth and the only
execution authority, and it is PAPER ONLY. Jarvis can read, explain, research, remember, schedule alerts,
run sandbox backtests and cut risk (HALT / PAUSE). It has no tool that places, changes or cancels an order.
"""
import engine  # noqa: F401  (puts kit/ on sys.path: policy, tools, voice_router, quality_gate)
