"""Core domain, persistence, and trading services for Algo Trading Control Center.

Architectural Rule:
This package MUST remain completely independent of FastAPI and Streamlit so that
presentation or transport layers can evolve (e.g., Streamlit -> React) without
rewriting domain or trading logic.
"""

__version__ = "0.1.0"
