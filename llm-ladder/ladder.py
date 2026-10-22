"""Provider ladder for text completion (stdlib only, no network, no vendor SDKs).

You supply providers as plain callables ``fn(prompt) -> str | None`` ordered from
preferred to last resort (e.g. CLI -> hosted API -> local model).  ``complete``
returns the first answer that is non-empty, is not just an API error string, and
passes your optional ``validate`` hook.  It never raises for provider trouble:
if every rung fails it returns ``None`` so the caller falls back to its default
behaviour.  Why each rung failed is kept in ``last_result()``.

Rules baked in:
* A provider that returns only an error line (e.g. ``API Error: 400``) has
  *failed* - that string must never be used as the answer.
* Input is never silently truncated.  If ``max_input_chars`` is set and the
  prompt is longer, ``InputTooLongError`` is raised (a caller bug, not a
  provider failure).  Chunk it yourself.
* Partial silent failure is made visible with ``report_coverage``.
"""

import ast
import json
import re
import threading
import time

__all__ = ["Provider", "Result", "InputTooLongError", "complete", "complete_ex",
           "last_result", "looks_like_api_error", "report_coverage", "mostly_korean",
           "parse_json_list"]


class InputTooLongError(ValueError):
    """Prompt exceeds ``max_input_chars``.  Raised instead of truncating."""


class Provider:
    """Named rung.  ``fn(prompt) -> str | None``; ``tags`` lets ``allow=`` filter rungs."""

    def __init__(self, name, fn, tags=()):
        self.name = str(name)
        self.fn = fn
        self.tags = frozenset(tags)


class Result:
    """Outcome of one ``complete_ex`` call."""

    def __init__(self):
        self.text = None
        self.rung = None          # name of the provider that answered
        self.failures = []        # [(provider name, reason)]
        self.skipped = []         # names excluded by allow=
        self.seconds = 0.0

    @property
    def ok(self):
        return self.text is not None

    def __repr__(self):
        return "Result(rung=%r, failures=%r, skipped=%r)" % (self.rung, self.failures, self.skipped)


_tls = threading.local()

# --- what counts as "the whole output is an API error" ------------------------------
# HEURISTIC, not a parser.  Only SHORT single-line outputs (or one JSON/dict object)
# are considered; long answers are never classified as errors.  A short output is an
# error only if it matches one of:
#   (a) an error body   {"type":"error",...}  or  {"error":{"type":..., "message":...}}
#                       (JSON or Python-dict style, e.g. the SDK's "Error code: 429 - {...}")
#   (b) a status line   "API Error: 400", "Error code: 529", "RateLimitError: 429 ...",
#                       "anthropic.InternalServerError: 500 ...", "HTTP 503 Service Unavailable",
#                       "429 Too Many Requests".  After the status number the line must end,
#                       or continue with a known reason phrase, " - ", or ":" + a known error
#                       word.  (Class-style names such as FooError: 500 may continue freely.)
#   (c) a bare token    "Overloaded", "Error: Overloaded", "Service Unavailable",
#                       "Too Many Requests", "Rate limit exceeded"
#   (d) a short vendor limit/credit line  "Credit balance is too low ...", "usage limit reached"
# Free text that merely starts with the word "error" is NOT an error.
_STATUS = r"([45]\d\d)(?!\d)"
_REASONS = (
    "bad request", "unauthorized", "payment required", "forbidden", "not found",
    "method not allowed", "request timeout", "conflict", "gone", "payload too large",
    "request entity too large", "unprocessable entity", "too many requests",
    "request header fields too large", "unavailable for legal reasons",
    "internal server error", "not implemented", "bad gateway", "service unavailable",
    "gateway timeout", "gateway time-out", "http version not supported",
    "insufficient storage", "network authentication required",
)
_TAIL_EXTRA = ("overloaded", "overloaded_error", "rate limit", "rate limited", "rate_limit",
               "slow down", "quota", "invalid request", "invalid_request")
_REASON_ALT = "|".join(re.escape(r) for r in _REASONS + _TAIL_EXTRA)
_ERRWORD = ("error|exception|failed|failure|invalid|denied|rejected|refused|timeout|"
            "timed out|unavailable|overloaded|exceeded|limit")
_END = (r"\)?(?:\s*[.!]?$"                                   # status ends the line
        r"|[\s:,;\-]+(?:" + _REASON_ALT + r")\b.{0,500}$"    # known reason phrase
        r"|\s+-\s.{0,500}$"                                  # " - <body>"
        r"|\s*:\s*(?:" + _ERRWORD + r")\b.{0,500}$)")        # ":" + error word
_STATUS_LINES = (
    # API Error: 400   API Error (503) overloaded   Error: 429 too many requests   Error code: 529
    re.compile(r"^(?:api\s*error\s*[:\-(]?|error\s*code\s*[:\-(]?|error\s*[:\-(])\s*"
               + _STATUS + _END, re.IGNORECASE),
    # RateLimitError: 429 ...   anthropic.InternalServerError: 500 ...  (real exception class)
    re.compile(r"^(?:[A-Za-z_]\w*\.)*[A-Z][A-Za-z]+Error\s*[:\-(]\s*" + _STATUS
               + r"\)?(?:[\s:,;\-].{0,500})?$"),
    # Internal Server Error: 500   Bad Gateway Error - 502
    re.compile(r"^(?:[A-Z][a-z]+\s+){1,3}Error\s*[:\-(]\s*" + _STATUS + _END),
    # HTTP 503 Service Unavailable   HTTP/1.1 429   HTTP error: 502
    re.compile(r"^http(?:/\d(?:\.\d)?)?\s*(?:error\s*)?[:\-]?\s*" + _STATUS + _END,
               re.IGNORECASE),
)
_REASON_LINE = re.compile(r"^" + _STATUS + r"\s+(?:" + "|".join(_REASONS) + r")\s*[.!]?$",
                          re.IGNORECASE)
_BARE_TOKENS = re.compile(
    r"^(?:error\s*[:\-]\s*)?(?:overloaded(?:_error)?|too many requests|rate limit exceeded|"
    r"rate_limit_error|service unavailable|bad gateway|gateway timeout|"
    r"internal server error)\s*[.!]?$", re.IGNORECASE)
_VENDOR_LIMIT = re.compile(
    r"^(?:claude(?:\s+ai)?\s+)?(?:your\s+)?(?:"
    r"credit balance is too low|usage limit (?:reached|exceeded)|"
    r"(?:you(?:'ve|\s+have)\s+)?(?:reached|hit|exceeded)\s+(?:your\s+)?"
    r"(?:current\s+)?(?:usage|rate|quota)\s+limit|"
    r"(?:rate|quota) limit (?:reached|exceeded)|quota exceeded|"
    r"(?:you\s+)?exceeded your current quota)\b", re.IGNORECASE)


def _error_body(d):
    if not isinstance(d, dict):
        return False
    if d.get("type") == "error":
        return True
    err = d.get("error")
    return isinstance(err, dict) and bool(err.get("type")) and bool(err.get("message"))


def _json_error(t):
    if not (t.startswith("{") and t.endswith("}")) or len(t) > 4000:
        return False
    try:
        return _error_body(json.loads(t))
    except ValueError:
        pass
    try:                                       # Python-dict style: {'type': 'error', ...}
        return _error_body(ast.literal_eval(t))
    except Exception:
        return False


def looks_like_api_error(text):
    """True if the *entire* output is a short API error (heuristic, see the rules above).

    A long answer, or one that merely talks about errors ("Error handling means ..."),
    is never flagged.
    """
    t = (text or "").strip()
    if not t:
        return False
    if _json_error(t):
        return True
    if "\n" in t or len(t) > 600:
        return False
    if any(r.match(t) for r in _STATUS_LINES):
        return True
    if len(t) > 240:
        return False
    return bool(_REASON_LINE.match(t) or _BARE_TOKENS.match(t) or _VENDOR_LIMIT.match(t))


def _normalize(providers):
    out = []
    for i, p in enumerate(providers or []):
        if isinstance(p, Provider):
            out.append(p)
        elif isinstance(p, tuple) and len(p) >= 2:
            out.append(Provider(p[0], p[1], p[2] if len(p) > 2 else ()))
        elif callable(p):
            out.append(Provider(getattr(p, "__name__", "provider%d" % i), p))
        else:
            raise TypeError("provider %d is not callable" % i)
    return out


def complete_ex(prompt, providers, *, allow=None, validate=None, max_input_chars=None,
                logger=None):
    """Try providers in order; return a ``Result`` (``result.text`` is None on total failure).

    allow: None = every rung; a set/list of provider names or tags = only those
           rungs (others are recorded in ``result.skipped``); a callable
           ``f(provider) -> bool`` is also accepted.
    validate: ``f(text) -> bool``; a False/raising validator counts as a failure.
    """
    log = logger or (lambda m: None)
    if max_input_chars is not None and len(prompt or "") > int(max_input_chars):
        raise InputTooLongError("prompt is %d chars, limit %d; split it, it will not be truncated"
                                % (len(prompt), int(max_input_chars)))
    res = Result()
    t0 = time.time()
    for p in _normalize(providers):
        if allow is not None:
            if callable(allow):
                permitted = bool(allow(p))
            else:
                permitted = p.name in allow or bool(p.tags & set(allow))
            if not permitted:
                res.skipped.append(p.name)
                continue
        try:
            out = p.fn(prompt)
        except Exception as e:                      # providers are untrusted: never propagate
            res.failures.append((p.name, "raised %s: %s" % (type(e).__name__, e)))
            log("[ladder] %s raised %r" % (p.name, e))
            continue
        if out is None or not isinstance(out, str) or not out.strip():
            res.failures.append((p.name, "empty"))
            continue
        if looks_like_api_error(out):
            res.failures.append((p.name, "api_error: %s" % out.strip()[:120]))
            log("[ladder] %s returned an error line, treated as failure" % p.name)
            continue
        if validate is not None:
            try:
                good = bool(validate(out))
            except Exception as e:
                res.failures.append((p.name, "validator raised %s" % type(e).__name__))
                continue
            if not good:
                res.failures.append((p.name, "validation_failed"))
                continue
        res.text, res.rung = out.strip(), p.name
        break
    res.seconds = time.time() - t0
    _tls.last = res
    if not res.ok:
        log("[ladder] all rungs failed: %r" % (res.failures,))
    return res


def complete(prompt, providers, *, allow=None, validate=None, max_input_chars=None,
             logger=None):
    """First good answer as a string, or None.  Never raises for provider failures."""
    return complete_ex(prompt, providers, allow=allow, validate=validate,
                       max_input_chars=max_input_chars, logger=logger).text


def last_result():
    """The ``Result`` of the most recent call in this thread (or None)."""
    return getattr(_tls, "last", None)


def report_coverage(label, asked, answered, *, min_ratio=0.5, logger=None, on_bad=None):
    """Always log ``"<label>: N of M answered"`` so silent partial failure is visible.

    Returns True when coverage is bad: asked > 0 and (answered == 0 or
    answered < asked * min_ratio).  ``on_bad(label, asked, answered)`` is
    called in that case (hook your alerting here).
    """
    log = logger or print
    asked, answered = int(asked or 0), int(answered or 0)
    log("[%s] %d of %d answered" % (label, answered, asked))
    bad = asked > 0 and (answered == 0 or answered < asked * float(min_ratio))
    if bad and on_bad is not None:
        try:
            on_bad(label, asked, answered)
        except Exception as e:
            log("[%s] on_bad hook raised %r" % (label, e))
    return bad


def mostly_korean(text, min_ratio=0.5, max_cjk=3):
    """Example validator: Hangul must be >= min_ratio of letters, few Han characters.

    Small local models sometimes answer in Chinese to a Korean prompt; use this
    as ``validate=mostly_korean`` for text a person will read.
    """
    t = text or ""
    hangul = sum(1 for c in t if "가" <= c <= "힣")
    han = sum(1 for c in t if "一" <= c <= "鿿")
    letters = sum(1 for c in t if c.isalpha())
    if not letters:
        return False
    return hangul >= letters * float(min_ratio) and han <= int(max_cjk)


def parse_json_list(text):
    """Extract a list of objects from model output, or None.

    Accepts a bare array, ``{"items": [...]}``, a single object, or an array cut
    off mid-way (complete leading elements are kept).  Lets callers count
    "answered" honestly instead of swallowing a wrong shape as an empty list.
    """
    import json
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        v = json.loads(t)
    except ValueError:
        v = None
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        for k in ("items", "results", "data"):
            if isinstance(v.get(k), list):
                return v[k]
        return [v]
    start = t.find("[")
    if start < 0:
        return None
    dec, i, items = json.JSONDecoder(), start + 1, []
    while i < len(t):
        while i < len(t) and t[i] in " \n\r\t,":
            i += 1
        if i >= len(t) or t[i] == "]":
            break
        try:
            obj, i = dec.raw_decode(t, i)
        except ValueError:
            break
        items.append(obj)
    return items or None
