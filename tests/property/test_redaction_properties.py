import base64
import string

from hypothesis import given
from hypothesis import strategies as st

from heimdall.tools.redact import redact
from heimdall.tools.sanitize import sanitize

token_chars = string.ascii_letters + string.digits
log_text = st.text(alphabet=string.ascii_lowercase + " .:=/-_\n", max_size=200)


@st.composite
def planted_secret(draw: st.DrawFn) -> str:
    """A secret in one of the formats workloads actually leak."""
    body = draw(st.text(alphabet=token_chars, min_size=36, max_size=60))
    kind = draw(st.sampled_from(["ghp", "sk", "aws", "jwt", "password", "url"]))
    if kind == "ghp":
        return "ghp_" + body
    if kind == "sk":
        return "sk-" + body
    if kind == "aws":
        return "AKIA" + draw(
            st.text(alphabet=string.ascii_uppercase + string.digits, min_size=16, max_size=16)
        )
    if kind == "jwt":
        part = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
        return f"eyJ{part}.{part}.{part}"
    if kind == "password":
        return "password=" + body
    return f"https://user:{body}@host.example/path"


def secret_value(secret: str) -> str:
    """The part that must not survive (not the `password=` key or the URL)."""
    if secret.startswith("password="):
        return secret.removeprefix("password=")
    if secret.startswith("https://"):
        return secret.split(":")[2].split("@")[0]
    return secret


@given(before=log_text, secret=planted_secret(), after=log_text)
def test_a_planted_secret_never_survives(before: str, secret: str, after: str) -> None:
    out = redact(f"{before} {secret} {after}")

    assert secret_value(secret) not in out


@given(text=st.text(max_size=400))
def test_redaction_is_idempotent(text: str) -> None:
    once = redact(text)
    assert redact(once) == once


@given(text=st.text(max_size=2000), limit=st.integers(min_value=50, max_value=500))
def test_sanitized_output_has_no_control_characters_and_is_bounded(text: str, limit: int) -> None:
    out, flagged = sanitize(text, limit=limit)

    assert not any(ord(c) < 32 and c not in "\n\t" for c in out)
    assert "\x1b" not in out
    # Budget, plus the truncation notice, plus one marker per flagged line.
    assert len(out) <= limit + 60 + flagged * 40
