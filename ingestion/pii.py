"""PII detection/masking for free-text fields (e.g. the `tags` column).

Structured dimension fields (linked_account_id, service, usage_type, region)
are NOT scanned here - they're validated, typed identifiers the pipeline
needs intact downstream. PII realistically leaks through free-text fields
where someone pasted an email, an ARN, or an account number into a tag value.
"""
import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ARN_RE = re.compile(r"arn:aws:[a-zA-Z0-9\-]*:[a-zA-Z0-9\-]*:\d{12}:[\w\-/:.*]+|arn:aws:[a-zA-Z0-9\-]*:::[\w\-/.*]+")
ACCOUNT_ID_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")

# Order matters: ARNs contain account IDs, so mask ARNs first or the account-id
# pattern would eat the digits inside the ARN before ARN_RE gets to match it.
PATTERNS = [
    ("ARN", ARN_RE),
    ("EMAIL", EMAIL_RE),
    ("ACCOUNT_ID", ACCOUNT_ID_RE),
]


def mask_pii(text: str) -> tuple[str, list[str]]:
    """Returns (masked_text, list of PII types found)."""
    found = []
    for label, pattern in PATTERNS:
        if pattern.search(text):
            found.append(label)
            text = pattern.sub(f"[REDACTED_{label}]", text)
    return text, found
