"""Research loop primitives for the public workbench."""

from quant_forge.research_loop.config import ResearchLLMConfig, ResearchLoopConfig, load_research_loop_config
from quant_forge.research_loop.service import (
    ResearchEffectiveTrialConfig,
    ResearchGenerationMetadata,
    ResearchGate,
    ResearchLoopService,
    ResearchObjectiveWeights,
    ResearchSearchTraceEntry,
    ResearchSelfReview,
    ResearchTrialSimulationOverlay,
    weighted_split_icir,
)
from quant_forge.research_loop.reporting import render_research_report, write_research_report
from quant_forge.research_loop.scheduler import ResearchLoopScheduler, ResearchScheduleRequest

__all__ = [
    "ResearchGenerationMetadata",
    "ResearchEffectiveTrialConfig",
    "ResearchGate",
    "ResearchLLMConfig",
    "ResearchLoopConfig",
    "ResearchLoopScheduler",
    "ResearchLoopService",
    "ResearchObjectiveWeights",
    "ResearchScheduleRequest",
    "ResearchSearchTraceEntry",
    "ResearchSelfReview",
    "ResearchTrialSimulationOverlay",
    "render_research_report",
    "load_research_loop_config",
    "weighted_split_icir",
    "write_research_report",
]
