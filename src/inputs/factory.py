"""Build input sources from PipelineConfig in one place."""

from __future__ import annotations

from ..config import DATE_RANGE_DAYS, PipelineConfig
from ..utils.logger import get_logger
from .base import InputSource
from .cloudwatch_api import CloudWatchSource
from .cost_explorer_api import CostExplorerSource
from .local_csv import LocalCSVSource

log = get_logger("inputs.factory")


def build_sources(cfg: PipelineConfig) -> list[InputSource]:
    sources: list[InputSource] = []
    # Cost Explorer's `days` window plays the same role as LocalCSVSource's
    # date_range, just expressed as an int; default 30 matches its own default.
    ce_days = DATE_RANGE_DAYS.get(cfg.date_range, 30)
    for kind in cfg.sources:
        if kind == "local_csv":
            sources.append(LocalCSVSource(cfg.docs_folder, date_range=cfg.date_range))
        elif kind == "cost_explorer":
            sources.append(CostExplorerSource(region=cfg.aws_region, days=ce_days))
        elif kind == "cloudwatch":
            sources.append(CloudWatchSource(region=cfg.aws_region))
        else:
            log.warning("Unknown source kind: %s (skipping)", kind)
    return sources
