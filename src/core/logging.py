import logging


def configure_logging() -> None:
    """Configure a conservative structured baseline for local and container execution."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
