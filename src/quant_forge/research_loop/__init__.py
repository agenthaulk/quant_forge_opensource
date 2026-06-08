"""Research loop primitives for the public workbench."""

from quant_forge.research_loop.campaign import (
    ResearchCampaignCandidate,
    ResearchCampaignOptimizerMetadata,
    ResearchCampaignResult,
    ResearchCampaignRoundResult,
    ResearchCampaignService,
)
from quant_forge.research_loop.config import ResearchLLMConfig, ResearchLoopConfig, load_research_loop_config
from quant_forge.research_loop.service import (
    ResearchGenerationMetadata,
    ResearchGate,
    ResearchLoopService,
    ResearchObjectiveWeights,
    ResearchSearchTraceEntry,
    ResearchSelfReview,
    weighted_split_icir,
)
from quant_forge.research_loop.reporting import render_research_report, write_research_report
from quant_forge.research_loop.scheduler import ResearchLoopScheduler, ResearchScheduleRequest

__all__ = [
    "ResearchCampaignCandidate",
    "ResearchCampaignOptimizerMetadata",
    "ResearchCampaignResult",
    "ResearchCampaignRoundResult",
    "ResearchCampaignService",
    "ResearchGenerationMetadata",
    "ResearchGate",
    "ResearchLLMConfig",
    "ResearchLoopConfig",
    "ResearchLoopScheduler",
    "ResearchLoopService",
    "ResearchObjectiveWeights",
    "ResearchScheduleRequest",
    "ResearchSearchTraceEntry",
    "ResearchSelfReview",
    "render_research_report",
    "load_research_loop_config",
    "weighted_split_icir",
    "write_research_report",
]
