"""Build the composite area description fed to the AI relevance assessor for
a custom region (uk/pipeline_custom.py) — a web-knowledge overview of the
region itself, plus the already-cached description of each member
constituency.

Both halves reuse libby_core.descriptions.ensure_description UNMODIFIED:
the region's own overview is cached exactly the same way, just keyed by area
name instead of a PCON code; a member constituency's description is read
from (or, if missing, generated into and added to) the very same
DESCRIPTIONS_PATH cache a normal constituency run already uses — so running
the constituency pipeline for a seat later never regenerates its
description, and vice versa.
"""

import logging

import pandas as pd

from libby_core import descriptions
from uk.settings import CONSTITUENCIES_PATH, CUSTOM_AREA_DESCRIPTIONS_PATH, DESCRIPTIONS_PATH

logger = logging.getLogger(__name__)

REGION_AREA_KIND = "informal UK region (spanning multiple parliamentary constituencies)"
CONSTITUENCY_AREA_KIND = "UK parliament constituency"


def ensure_region_overview(area_name: str, force: bool = False) -> str:
    """The region's own web-knowledge description, cached in
    CUSTOM_AREA_DESCRIPTIONS_PATH keyed by area name (there's no PCON code to
    key it by)."""
    if force:
        _clear_cached_region_overview(area_name)
    return descriptions.ensure_description(
        area_id=area_name,
        area_name=area_name,
        path=CUSTOM_AREA_DESCRIPTIONS_PATH,
        area_kind=REGION_AREA_KIND,
    )


def _clear_cached_region_overview(area_name: str) -> None:
    if not CUSTOM_AREA_DESCRIPTIONS_PATH.exists():
        return
    existing = descriptions.load_descriptions(CUSTOM_AREA_DESCRIPTIONS_PATH)
    remaining = existing[existing["area_id"] != str(area_name)]
    if len(remaining) != len(existing):
        remaining.to_csv(CUSTOM_AREA_DESCRIPTIONS_PATH, index=False)


def _pcon_code_for(pcon24nm: str, constituencies_df: pd.DataFrame) -> str:
    match = constituencies_df[constituencies_df["PCON24NM"] == pcon24nm]
    return str(match.iloc[0]["PCON24CD"]) if not match.empty else pcon24nm


def build_composite_description(
    area_name: str,
    member_constituencies: list[str],
    force_overview: bool = False,
) -> str:
    """Region overview + a rendered list of member constituencies, each with
    its own (cache-first) description. This full text is what gets passed as
    `area_description` to libby_core.assessment.assess_groups — the AI
    assessor judges "too broad for this area" against the whole of this
    text, not just the region's own summary line."""
    overview = ensure_region_overview(area_name, force=force_overview)
    if not overview:
        logger.warning("No region overview generated for '%s' — composite description will be constituency-list-only", area_name)

    constituencies_df = pd.read_csv(CONSTITUENCIES_PATH)

    lines = []
    for pcon24nm in member_constituencies:
        pcon24cd = _pcon_code_for(pcon24nm, constituencies_df)
        desc = descriptions.ensure_description(
            area_id=pcon24cd,
            area_name=pcon24nm,
            path=DESCRIPTIONS_PATH,
            area_kind=CONSTITUENCY_AREA_KIND,
            id_col="PCON24CD",
            name_col="PCON24NM",
        )
        lines.append(f"- {pcon24nm}: {desc}" if desc else f"- {pcon24nm}")

    member_block = "\n".join(lines)
    return (
        f"{overview}\n\n"
        f"This region is made up of the following UK parliamentary constituencies:\n"
        f"{member_block}"
    ).strip()
