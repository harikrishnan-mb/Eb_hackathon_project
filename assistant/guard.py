"""Pre-execution plugins: pii_redactor and injection_shield (design §1.8, fail_closed).

Both run before any retrieval or LLM call. On an internal exception they
raise PlatformError(DEGRADED) so the API returns 503 (design FMEA row 10).

These are rule-based first versions. KPI-02 (PII recall ≥ 99.8%) and KPI-03
(injection block ≥ 99.5%) need the benchmark sets and are not yet met.
"""

from __future__ import annotations

import re
import unicodedata

from .errors import ErrorCode, PlatformError

_PII = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("obfuscated_email", re.compile(r"[A-Za-z0-9._%+-]+\[at\][A-Za-z0-9.\[\]-]+")),
    ("phone", re.compile(r"\(?\b\d{3}\)?[ -]?\d{3}-\d{4}\b")),
    ("phone", re.compile(r"\(\d{3}\)\s?\d{3,4}[- ]\d{4}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
]

_INJECTION = re.compile(
    r"ignore (all |any )?(previous|prior|above) instructions"
    r"|system prompt|developer mode|reveal your (hidden |internal )?(rules|reasoning)"
    r"|no restrictions|<instruction>|SYSTEM:",
    re.IGNORECASE,
)


# SQL injection and tool-argument overflow (red-team categories sqli_overflow).
_SQLI = re.compile(
    r"'\s*;|;\s*--|--\s*$|\bdrop\s+table\b|\bdelete\s+from\b|\binsert\s+into\b"
    r"|\bunion\s+(all\s+)?select\b|\bxp_\w+|\bexec(ute)?\s*\("
    r"|'\s*(or|and)\s+\S+\s*=\s*\S+",
    re.IGNORECASE,
)
_RUN = re.compile(r"(.)\1{63,}")  # a single character repeated 64+ times
MAX_TOKEN_LEN = 200                # a single non-space token this long is an overflow attempt

# Crescendo: escalation phrases that ask for more detail, hidden reasoning, or a role change.
# --- Rules v2 (written from general attack categories, see reports/redteam/rules_v2_*) ---
# Instruction override and persona/mode switching.
_OVERRIDE = re.compile(
    r"\b(you are now|from now on|act as|pretend (you are|to be)|roleplay|role[- ]play|"
    r"respond as|answer as|you will be)\b.{0,80}\b(dan|omnibot|unrestricted|unfiltered|no (rules|restrictions|guidelines|filters|content policy)|"
    r"free agent|without (any )?(rules|limits|filters)|jailbr\w*|developer mode|evil)\b"
    r"|\b(maintenance|debug|developer|admin|god|jailbreak) mode\b"
    r"|\b(disregard|ignore|forget|override|bypass|skip|turn off|disable|suspend)\b.{0,40}"
    r"\b(guidance|guidelines|rules|instructions|safety|policy|policies|checks?|filters?|restrictions|review|reviewer|layer|safeguards?|threshold)\b"
    r"|\b(previous|prior|earlier|above) (instructions|rules|guidance)\b.{0,30}\b(expired|void|cancelled|canceled|replaced|superseded|do not apply)\b"
    r"|\bpolicy checks are optional\b|\bno (content )?polic(y|ies)\b",
    re.IGNORECASE,
)
# Prompt extraction, including encoded or translated requests.
_EXTRACT_V2 = re.compile(
    r"\b(initial|original|core|hidden|internal|underlying|secret)\b.{0,30}\b(directive|guidance|instructions?|configuration|config|prompt|rules|policy|settings)\b.{0,40}"
    r"\b(print|show|display|output|reveal|dump|repeat|translate|quote|recite|tell|list|write|reply|say)\b"
    r"|\b(print|show|display|output|reveal|dump|repeat|translate|quote|recite|reply|say|respond)\b.{0,40}"
    r"\b(initial|original|core|hidden|internal|underlying|secret|full)\b.{0,30}\b(directive|guidance|instructions?|configuration|config|prompt|rules|settings)\b"
    r"|\b(base64|rot13|hex|binary|pig latin|backwards|reversed)\b.{0,60}\b(instructions?|prompt|rules|directive|configuration|guidance)\b"
    r"|\b(password|token|secret|credential|api key)\b.{0,40}\b(poem|acrostic|story|riddle|song|letters|first letters|hint)\b"
    r"|\b(grandmother|grandma|grandfather|late)\b.{0,80}\b(approval|secret|admin|password|codes?|credentials?)\b",
    re.IGNORECASE,
)
# Escalation / crescendo follow-ups, including the reviewer and threshold probes.
_CRESCENDO_V2 = re.compile(
    r"\b(next|now|harder|deeper|further|more granular|more technical|escalate|even more detailed|one step further|step deeper|continue)\b.{0,60}"
    r"\b(override|bypass|skip|disable|approval|reviewer|threshold|thresholds|guardrail|guardrails|internal|refusal|escalation|checks|tools|endpoints|token|chain of thought|reasoning|what you were told|validated|validation)\b"
    r"|\b(internal|hidden|private) (thoughts|reasoning|notes|chain of thought)\b"
    r"|\bwhich (tools|endpoints) (could|can|do you) .{0,40}(without|skip|bypass)\b"
    r"|\bexact wording\b.{0,30}\brefusal\b|\bdo your checks depend on\b|\bonly answer yes or no\b.{0,80}\b(checks|role|reviewer|approval)\b",
    re.IGNORECASE,
)
# Embedded instructions in retrieved or quoted content (indirect injection).
_EMBEDDED_V2 = re.compile(
    r"\[(ai|assistant|model|system)[ _-]?(note|instruction|directive)?\]"
    r"|<!--\s*(assistant|ai|model|system)\b"
    r"|\bto the (ai|assistant|model)\b.{0,40}\b(reading|processing|summari[sz]ing)\b"
    r"|\b(important|note) for (models?|assistants?|ai)\b"
    r"|\bai systems? must\b|\bassistant:\s"
    r"|\b(action required|instruction)\b.{0,20}\b(by|for|to) the (assistant|ai|model)\b"
    r"|\b(reviewer|admin|system) (instruction|note|directive)\s*[:,]\s*(set|approve|mark|grant|reset|override|release|disable|transfer|delete)\b"
    r"|\b(hidden (text|note|instruction)s?)\b.{0,60}\b(override|approve|bypass|reveal|send|forward|grant)\b"
    r"|\b(follow|execute|carry out|perform|do)\b.{0,20}\b(instructions?|steps?|task|last line|footnote|commands?)\b.{0,60}\b(hidden|footnote|described|listed|in this|it lists|its|the document|the chunk|the sheet|the note)\b"
    r"|\b(footer|appendix|footnote)\b.{0,60}\b(must|should|shall)\b.{0,60}\b(output|reveal|list|send|forward|approve|ignore)\b"
    r"|\bfrom now on\b.{0,60}\b(prefix|append|include|add|prepend)\b"
    r"|\b(email|send|forward|transfer|wire)\b.{0,60}\b(customer list|conversation history|card number|admin token|internal discount)\b"
    r"|\bstep\s*\d+\s*:\s*(mark|set|approve|release|delete|transfer|send|update|disable|grant|wire|forward|reset|run|execute)\b",
    re.IGNORECASE,
)
# SQL injection: boolean tautologies, comment terminators, stacked and time-based payloads.
_SQLI_V2 = re.compile(
    r"\b(and|or)\s+'?\d+'?\s*=\s*'?\d+'?|\b(and|or)\s+'\w*'\s*=\s*'\w*'"
    r"|['\"]\s*(or|and)\b[^\n]{0,30}?(=|like)"
    r"|--\s*\S*$|#\s*$|/\*"
    r"|\border\s+by\s+\d+\b|\bsleep\s*\(|\bwaitfor\s+delay\b|\bbenchmark\s*\("
    r"|\b(update|truncate|drop|alter|create|grant)\s+(table|database|user|role|users?|set)\b"
    r"|\bselect\b.{0,80}\bfrom\b.{0,40}\b(where|users|refunds|orders|accounts|sessions)\b"
    r"|\bshutdown\b|\bxp_\w+|\b0x[0-9a-f]{4,}\b"
    r"|\binsert\s+into\b|\bdelete\s+from\b|\bunion\b.{0,20}\bselect\b",
    re.IGNORECASE,
)
# Obfuscated PII: spelled-out separators and digit words.
_DIGIT_WORDS = r"(zero|one|two|three|four|five|six|seven|eight|nine)"
_PII_V2 = re.compile(
    r"\b[\w.+-]+\s*(\(at\)|\[at\]|\s\bat\b\s)\s*[\w-]+\s*(\(dot\)|\[dot\]|\s\bdot\b\s)\s*(com|org|net|io|co|uk|edu|gov)\b"
    r"|\b[A-Za-z0-9._%+-]+\s+AT\s+[A-Za-z0-9-]+\s+DOT\s+(com|org|net|io|co|uk)\b"
    r"|\b\d{3}\s\d{2}\s\d{4}\b"
    r"|(\b" + _DIGIT_WORDS + r"\b[\s,\-]*){7,}",
    re.IGNORECASE,
)

# Direct jailbreak: asks to output or repeat the system message, prompt, or instructions.
_EXTRACT = re.compile(
    r"\b(output|print|show|repeat|reveal|display|dump|quote|recite|tell me)\b.{0,60}"
    r"\b(system|hidden|initial|original|internal)\s+(message|prompt|instructions?|rules)\b"
    r"|\b(exact|verbatim|word[- ]for[- ]word)\b.{0,60}\b(system|instructions?|prompt|rules)\b",
    re.IGNORECASE,
)
# Indirect injection: a document or chunk carries imperative steps for the assistant to run.
_EMBEDDED = re.compile(
    r"\b(read|open|process|summari[sz]e|parse)\b.{0,80}\b(chunk|document|note|passage|page|file)\b"
    r".{0,80}\b(follow|execute|carry out|perform|do)\b"
    r"|\bstep\s*\d+\s*:\s*(mark|set|approve|release|delete|transfer|send|update|disable|grant|wire)\b",
    re.IGNORECASE,
)

_CRESCENDO = re.compile(
    r"one (level|step) more (detailed|specific)"
    r"|internal reasoning|your (hidden|internal|private) (thoughts|reasoning|notes)"
    r"|(good|great|perfect|nice)[.,!]? (now|and now)\b.{0,40}(more|deeper|detailed)"
    r"|play a game where you only answer"
    r"|pretend (you are|to be) (an? )?(unrestricted|unfiltered|different)",
    re.IGNORECASE,
)


def threat_category(text: str) -> str | None:
    """Return the first matching threat category, or None. Fail closed on error."""
    try:
        t = unicodedata.normalize("NFKC", text)
        if _INJECTION.search(t):
            return "injection"
        if _SQLI.search(t):
            return "sql_injection"
        if _RUN.search(t) or any(len(tok) > MAX_TOKEN_LEN for tok in t.split()):
            return "overflow"
        if _CRESCENDO.search(t):
            return "crescendo"
        if _EXTRACT.search(t):
            return "prompt_extraction"
        if _EMBEDDED.search(t):
            return "embedded_instruction"
        if _OVERRIDE.search(t):
            return "override"
        if _EXTRACT_V2.search(t):
            return "prompt_extraction"
        if _EMBEDDED_V2.search(t):
            return "embedded_instruction"
        if _CRESCENDO_V2.search(t):
            return "crescendo"
        if _SQLI_V2.search(t):
            return "sql_injection"
        if _PII_V2.search(t):
            return "obfuscated_pii"
        return None
    except Exception as exc:  # noqa: BLE001 - fail closed by design
        raise PlatformError(ErrorCode.DEGRADED, "injection_shield unavailable") from exc


def redact(text: str) -> tuple[str, list[str]]:
    """Return redacted text and the kinds of PII found. Fail closed on error."""
    try:
        normalized = unicodedata.normalize("NFKC", text)
        kinds: list[str] = []
        for kind, pattern in _PII:
            if pattern.search(normalized):
                kinds.append(kind)
                normalized = pattern.sub(f"[{kind.upper()}]", normalized)
        return normalized, kinds
    except Exception as exc:  # noqa: BLE001 - fail closed by design
        raise PlatformError(ErrorCode.DEGRADED, "pii_redactor unavailable") from exc


def check_injection(text: str) -> bool:
    """True if the text matches any threat category (injection, SQL, overflow, crescendo). Fail closed."""
    return threat_category(text) is not None
