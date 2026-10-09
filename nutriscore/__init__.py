"""Nutri-Score (2023 algorithm) for our USDA product records.

    from nutriscore import compute
    compute(record)  # {"score", "grade", "category_variant", "components", "version", "missing", "estimated", ...}

``algorithm`` holds the published point tables and rules; ``usda`` maps our records
onto them; ``validate`` compares the result with Open Food Facts. See docs/NUTRISCORE.md.
"""

from .algorithm import score
from .usda import VERSION, compute

__all__ = ["compute", "score", "VERSION"]
