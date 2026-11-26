from typing import NamedTuple


class EvaluationReference(NamedTuple):
    baseline_id: str
    baseline_generation: str
    candidate_generation: str
    label: str


# Explicit before/after pairs carried forward from the previous homepage.
# The score is always recomputed from the authorized, pinned batch generations.
EVALUATION_REFERENCES = {
    "b_da4ba4ef9c476d91b1f2": EvaluationReference(
        "b_f06f30c356c973d75426", "gen_legacy_0ce76e7872def6cd",
        "gen_legacy_08510fbff2dc2549", "Chat → Responses",
    ),
    "b_344aefc389bad47e526f": EvaluationReference(
        "b_063baf00fc22e9fa6a8a", "gen_legacy_3163bc26df982d64",
        "gen_legacy_ce3c238db6d11fbf", "Pi 运行设置对照",
    ),
}

APPROVED_REFERENCE_EVIDENCE = frozenset({"approved_public", "approved_restricted"})
