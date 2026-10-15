"""Jarvis trading engine (paper only).

Code decides; Jarvis (the AI) only proposes. Nothing in this package can place a real order:
there are no broker or exchange keys and no function that talks to a broker.
"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "kit")
if KIT not in sys.path:
    sys.path.insert(0, KIT)          # policy.py, quality_gate.py, voice_router.py, tools.py live in kit/
