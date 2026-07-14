"""Launch the MeteoEdge dashboard server on the local network.

Reuses the guarded start_dashboard() from src.monitoring.dashboard to ensure
graceful handling of port-binding conflicts (issue #722).
"""
from src.monitoring.dashboard import start_dashboard

if __name__ == "__main__":
    start_dashboard(host="0.0.0.0", port=8000)
