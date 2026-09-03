"""Accounting package — token math, attribution, pricing, sanitization.

Everything here is pure Python and deterministic so it can be unit-tested
without a database or network.
"""
from .tokenizer import Tokenizer, get_tokenizer, TokenCount, hash_text, hash_bytes
from .usage import (
    UsageSummary,
    UsageProvenance,
    extract_usage_summary,
    cache_hit_ratio,
    tokens_per_second,
)
from .sanitize import (
    sanitize_text,
    sanitize_dict,
    truncate_for_storage,
)
from .attribution import (
    ComponentAttribution,
    classify_message_role,
    attribute_messages,
    attribution_coverage,
    COMPONENT_ORDER,
)
from .pricing import (
    PricingTable,
    PricingEntry,
    Profile,
    load_pricing_profile,
    compute_cost,
)

__all__ = [
    "Tokenizer",
    "get_tokenizer",
    "TokenCount",
    "UsageSummary",
    "UsageProvenance",
    "extract_usage_summary",
    "cache_hit_ratio",
    "tokens_per_second",
    "sanitize_text",
    "sanitize_dict",
    "hash_bytes",
    "hash_text",
    "truncate_for_storage",
    "ComponentAttribution",
    "classify_message_role",
    "attribute_messages",
    "attribution_coverage",
    "COMPONENT_ORDER",
    "PricingTable",
    "PricingEntry",
    "Profile",
    "load_pricing_profile",
    "compute_cost",
]