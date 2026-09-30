"""Build input sources from PipelineConfig in one place."""

from __future__ import annotations

from ..config import PipelineConfig
from ..utils.logger import get_logger
from .base import InputSource
from .cloudwatch_api import CloudWatchSource
from .cost_explorer_api import CostExplorerSource

log = get_logger("inputs.factory")


def build_sources(cfg: PipelineConfig) -> list[InputSource]:
    sources: list[InputSource] = []
    for kind in cfg.sources:
        if kind == "cost_explorer":
            sources.append(CostExplorerSource(region=cfg.aws_region, date_range=cfg.date_range, tag_key=cfg.cost_tag_key))
        elif kind == "cloudwatch":
            sources.append(CloudWatchSource(region=cfg.aws_region))
        else:
            log.warning("Unknown source kind: %s (skipping)", kind)
    return sources
