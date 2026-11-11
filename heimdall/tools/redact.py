"""Secret redaction for tool output.

Runs on everything a tool returns before it can reach the model, a webhook or
a report. Workloads print secrets to their own logs more often than anyone
would like; this is what keeps those out of prompts and postmortems.

Two passes: known secret formats, then a generic check for long high-entropy
tokens that look like keys but match no known format.
"""

import math
import re
from collections import Counter

_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.DOTALL
)

# (kind, pattern). Order matters: more specific formats first.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("url_credentials", re.compile(r"(?<=://)[^\s:/@]+:[^\s:/@]+(?=@)")),
    ("bearer_token", re.compile(r"(?i)(?<=\bbearer\s)[A-Za-z0-9._~+/=-]{16,}")),
]

# `Authorization: <scheme> <credentials>` headers.
_AUTH_HEADER = re.compile(
    r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(?:(?:basic|bearer|digest|token)\s+)?[^\s\"',;]+"
)

# key=value and key: value pairs whose key names a secret. The value is
# replaced, the key is kept so the log line still makes sense.
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<key>["']?[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|
        access[_-]?key|private[_-]?key|credential)[\w.-]*["']?)
    (?P<sep>\s*[=:]\s*)
    (?P<value>\[redacted:[a-z_]+\]|"[^"\n]{3,}"|'[^'\n]{3,}'|[^\s,;&"'}\]]{3,})
    """
)

_CANDIDATE = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_ENTROPY_THRESHOLD = 4.0
_HEX = re.compile(r"\A[0-9a-fA-F]+\Z")
_MARKER = re.compile(r"\[redacted:[a-z_]+\]")


def _entropy(text: str) -> float:
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def _looks_random(token: str) -> bool:
    # Hex strings top out at 4 bits per character, so they get a lower bar;
    # but plain hex is also what container and commit IDs look like, and
    # those are evidence the investigator needs. Only mixed-case base64-like
    # tokens are treated as secrets here.
    if _HEX.match(token):
        return False
    has_classes = sum(
        [any(c.islower() for c in token), any(c.isupper() for c in token),
         any(c.isdigit() for c in token)]
    )  # fmt: skip
    return has_classes == 3 and _entropy(token) >= _ENTROPY_THRESHOLD


def redact(text: str) -> str:
    """Replace anything that looks like a secret with `[redacted:<kind>]`."""
    text = _PEM.sub("[redacted:private_key]", text)
    text = _AUTH_HEADER.sub(
        lambda m: m[0] if m[0].endswith("]") else f"{m[1]}[redacted:auth_header]", text
    )
    for kind, pattern in _PATTERNS:
        text = pattern.sub(f"[redacted:{kind}]", text)

    def assignment(match: re.Match[str]) -> str:
        if _MARKER.fullmatch(match["value"].strip("\"'")):
            return match[0]
        return f"{match['key']}{match['sep']}[redacted:secret]"

    text = _ASSIGNMENT.sub(assignment, text)

    def candidate(match: re.Match[str]) -> str:
        return "[redacted:high_entropy]" if _looks_random(match[0]) else match[0]

    return _CANDIDATE.sub(candidate, text)
