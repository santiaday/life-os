"""Parallel rater implementations. Each rater returns a dict shaped like:

    {"source": str, "overall": float | None, "dimensions": dict}

…or raises on failure. The service layer fans them out on a thread pool
so one rater failing never blocks the others — we record the failure
and save what succeeded.

GPT-4o is shipped but not wired into the default fan-out (no
OPENAI_API_KEY today). Drop it into service._run_raters_parallel when
you want a second opinion.
"""

from .claude import rate_claude
from .geometry import rate_geometry
from .gpt4v import rate_gpt4v  # available but not enabled by default

__all__ = ["rate_claude", "rate_geometry", "rate_gpt4v"]
