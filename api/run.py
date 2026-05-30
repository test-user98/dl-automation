"""Start the FastAPI server — Windows/Playwright-safe.

The agent launches Chromium via Playwright, which needs asyncio's Proactor
event loop to spawn the browser subprocess. uvicorn picks its loop from
`asyncio_loop_factory`, which returns a SelectorEventLoop whenever subprocess
mode is on (i.e. `--reload` or `--workers > 1`). A SelectorEventLoop CANNOT
spawn subprocesses, so the browser dies with NotImplementedError
("browser.launch_failed"). We therefore run single-process (reload OFF) so the
loop is Proactor. Restart this process manually to pick up code changes.

Run it with:  python -X utf8 -m api.run
"""
import uvicorn
from config.settings import get_settings

settings = get_settings()


def main() -> None:
    uvicorn.run(
        "api.server:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,           # MUST stay False on Windows — see module docstring.
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
