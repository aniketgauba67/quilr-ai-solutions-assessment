"""Run with python -m quilr_assessment.task1_mcp_server."""

import asyncio
import logging
import sys

from mcp.server.stdio import stdio_server

from .server import create_server

logger = logging.getLogger(__name__)


async def run() -> None:
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )
    logger.info("Starting Task 1 MCP server")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 0
    except Exception:
        logger.error("Task 1 MCP server stopped unexpectedly")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
