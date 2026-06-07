"""Launch the MeteoEdge dashboard server on the local network."""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "src.dashboard.api:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
