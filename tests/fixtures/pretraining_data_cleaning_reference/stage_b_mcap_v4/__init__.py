"""Four Stage B operators: normalization, statistics, judgment and delivery."""

from .contracts import (
    McapEpisodeRef, NormalizedEpisode, NormalizedRecord, NormalizedBatchRef,
    StatisticsContext, BatchQualityResult, BatchDeliveryResult, QualityDecision, QualityEpisode,
    SourceIdentity, StatisticsBundle,
)
from .operators import GlobalStatsOperator, McapReadOperator, QualityDetectionOperator
from .storage import load_batch, load_episode
from .delivery import McapDeliveryOperator
from .reporting import load_quality_batch

__version__ = "0.1.0"

__all__ = [
    "McapEpisodeRef", "QualityEpisode", "SourceIdentity", "StatisticsBundle", "QualityDecision",
    "McapReadOperator", "GlobalStatsOperator", "QualityDetectionOperator",
    "NormalizedEpisode", "NormalizedRecord", "NormalizedBatchRef", "StatisticsContext",
    "BatchQualityResult", "BatchDeliveryResult", "load_batch", "load_episode",
    "McapDeliveryOperator", "load_quality_batch",
]
