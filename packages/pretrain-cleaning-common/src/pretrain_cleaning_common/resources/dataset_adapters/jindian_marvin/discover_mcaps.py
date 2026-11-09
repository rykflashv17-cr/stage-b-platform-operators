"""Discover the delivered Jindian MCAPs in stable order."""

from pathlib import Path


def discover_mcaps(root: Path) -> list[Path]:
    root = Path(root)
    return sorted((*root.glob("*.mcap"), *root.glob("*/*.mcap")))
