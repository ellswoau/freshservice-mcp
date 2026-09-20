"""Entrypoint: `python -m freshservice_mcp`."""

from .server import main
import sys

if __name__ == "__main__":
    sys.exit(main())