"""Stateless Streamlit control and monitoring console for Algo Trading Control Center.

Architectural Rule:
This package MUST NOT import `core` or `backend`, own trading state, or run
trading/risk calculations. All state and actions flow exclusively through FastAPI.
"""
