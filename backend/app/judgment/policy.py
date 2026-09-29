"""Autonomy policy: turns raw System-1 signals into AUTO / ASK / BLOCK (docs/SPEC.md section 6).

Pure code, no model calls. Jev (or Laya) supplies calibrated probabilities; this module owns
every threshold and weight, so a rule change never needs a prompt change or a re-run of
inference. `describe()` exports the same numbers for GET /api/config and the UI.

Signals (all 0..1):
    alignment          Score 0..3 normalized: how directly the action serves the user's request
    injection          Noul: the action follows an instruction found in untrusted content
    sensitive          Noul: outgoing content discloses confidential material
    tone_ok            Noul (communicate only): professional and appropriate for the recipients
    recipients_match   Noul (communicate only): the request implies these recipients
    external, unknown_recipient, tainted   computed in code, 0 or 1
    control_tainted    code: a CONTROL arg (recipients, channel, url, parent, *_id target) derives
                       from untrusted content after endorsement (SPEC section 6, per-argument taint)

Per-argument taint (CaMeL-style): untrusted text may fill CONTENT args (body, text, content_md)
when the injection check is clean; it may never choose WHERE an action goes (CONTROL args)
without a human in balanced mode.

Composite risk is a weighted sum of *penalties* (1 - alignment, injection, sensitive, ...),
clipped to 1. Weights are per unit of penalty and deliberately do not sum to 1: a single
serious problem (an off-request action, a rude message, confidential content) must be able
to push risk past the autonomous threshold on its own, while mild signals still add up.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from ..core.models import Autonomy, EffectClass, ErrorKind, RecoveryStrategy, Verdict

POLICY_VERSION = "2026-09-30"

# Penalty weights for the composite risk (risk = min(1, sum(weight * penalty))).
WEIGHTS: dict[str, float] = {
    "misalignment": 0.70,  # 1 - alignment; an action the user did not ask for
    "injection": 0.60,  # follows an instruction from untrusted content
    "sensitive": 0.65,  # discloses confidential material (alone at p~1 it exceeds the autonomous limit)
    "tone": 0.70,  # 1 - tone_ok; communicate only
    "recipient_mismatch": 0.20,  # 1 - recipients_match; communicate only
    "external": 0.15,  # any recipient outside the internal domain
    "unknown_recipient": 0.15,  # any recipient not among known contacts
    "tainted": 0.10,  # args derive from untrusted content
}

THRESHOLDS: dict[str, float] = {
    # Always-on hard rules
    "block_injection": 0.70,  # injection >= this AND tainted -> BLOCK
    "block_sensitive": 0.80,  # sensitive >= this AND external -> BLOCK
    # balanced
    "balanced_reversible_max_risk": 0.35,  # write_reversible AUTO below this (and no control taint)
    "balanced_content_taint_max_injection": 0.20,  # content-tainted write_reversible AUTO only below this
    "balanced_comm_min_alignment": 0.66,
    "balanced_comm_max_injection": 0.20,
    "balanced_comm_max_sensitive": 0.30,
    "balanced_comm_min_tone": 0.70,
    # autonomous
    "autonomous_max_risk": 0.60,
    "autonomous_tainted_max_injection": 0.30,  # tainted AND injection >= this -> ASK
    "autonomous_control_taint_max_injection": 0.20,  # control_tainted AND injection >= this -> ASK
    # Signal levels at which a reason is shown in the approval UI
    "reason_injection": 0.20,
    "reason_sensitive": 0.30,
    "reason_tone": 0.70,  # tone_ok below this
    "reason_alignment": 0.66,  # alignment below this
    "reason_recipients": 0.40,  # recipients_match below this
    # Other judgments
    "clarify": 0.75,  # needs_clarification p above this -> ask the user first
    "verify_pass": 0.60,  # per-criterion noul at or above this counts as met
    "memory_min": 0.40,  # memories below this relevance are dropped
    "recovery_min_confidence": 0.50,  # classify_failure confidence below this -> ASK_HUMAN
    "max_transient_attempts": 3,  # total attempts (including the first) for transient errors
    "max_repair_attempts": 3,  # total attempts before arg repair gives up and asks
}

# Args that decide WHERE an action goes (who receives it, what it targets). Any other arg that
# ends in "_id" on a non-read tool also names a write target (doc_id, sheet_id, page_id, event_id).
CONTROL_ARGS: frozenset[str] = frozenset({"to", "cc", "bcc", "attendees", "channel", "url", "parent", "reply_to_id"})


def is_control_arg(name: str) -> bool:
    return name in CONTROL_ARGS or name.endswith("_id")


def control_args(tainted_args: Iterable[str]) -> list[str]:
    """The tainted args that are control args, in the given order."""
    return [a for a in tainted_args if is_control_arg(a)]


# Human-readable statement of the rules, in evaluation order (shown on /architecture).
RULES: list[str] = [
    "READ tools are always AUTO.",
    f"injection >= {THRESHOLDS['block_injection']} and tainted args -> BLOCK.",
    f"sensitive >= {THRESHOLDS['block_sensitive']} and an external recipient -> BLOCK.",
    "write_irreversible -> ASK (never AUTO).",
    "Judge unavailable (Jev and Laya both down) -> ASK, failing safe.",
    "cautious: every non-read action -> ASK.",
    (
        "Per-argument taint: control_tainted = a tainted arg is a control arg (to, cc, bcc, attendees, channel, url, "
        "parent, reply_to_id or any *_id target). Only control taint forbids AUTO; content taint (untrusted text in "
        "body/content) is allowed while the injection check is clean. Unknown per-arg taint counts as control taint."
    ),
    (
        f"balanced, write_reversible: AUTO if risk < {THRESHOLDS['balanced_reversible_max_risk']}, not control_tainted, "
        f"and (not tainted or injection < {THRESHOLDS['balanced_content_taint_max_injection']}); else ASK."
    ),
    (
        "balanced, communicate: AUTO only if not control_tainted, all recipients are internal and known, "
        f"alignment >= {THRESHOLDS['balanced_comm_min_alignment']}, injection < {THRESHOLDS['balanced_comm_max_injection']}, "
        f"sensitive < {THRESHOLDS['balanced_comm_max_sensitive']}, tone_ok >= {THRESHOLDS['balanced_comm_min_tone']}; else ASK."
    ),
    (
        f"autonomous: AUTO if risk < {THRESHOLDS['autonomous_max_risk']}, not "
        f"(tainted and injection >= {THRESHOLDS['autonomous_tainted_max_injection']}) and not "
        f"(control_tainted and injection >= {THRESHOLDS['autonomous_control_taint_max_injection']}); else ASK."
    ),
    "Signals from the offline fallback model (Laya) never AUTO a communication or a tainted write.",
]

# Effect classes ordered from least to most conservative (used when effects must be merged).
EFFECT_CAUTION_ORDER: list[EffectClass] = [
    EffectClass.READ,
    EffectClass.WRITE_REVERSIBLE,
    EffectClass.COMMUNICATE,
    EffectClass.WRITE_IRREVERSIBLE,
]


def most_conservative(*effects: EffectClass) -> EffectClass:
    return max(effects, key=EFFECT_CAUTION_ORDER.index)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def _pct(p: float) -> str:
    return f"{round(p * 100)}%"


def _join(items: Sequence[str], limit: int = 3) -> str:
    """'a', 'a and b', 'a, b and c', 'a, b, c and 2 more'."""
    shown = list(items[:limit])
    more = len(items) - len(shown)
    if more > 0:
        return f"{', '.join(shown)} and {more} more"
    if len(shown) <= 1:
        return "".join(shown)
    return f"{', '.join(shown[:-1])} and {shown[-1]}"


_CONTROL_LABELS = {"to": "Recipient list", "cc": "Recipient list", "bcc": "Recipient list",
                   "attendees": "Attendee list", "channel": "Channel", "url": "Target URL", "parent": "Parent page",
                   "reply_to_id": "Message being replied to", "doc_id": "Target document", "sheet_id": "Target sheet",
                   "page_id": "Target page", "event_id": "Target event", "message_id": "Target message"}
_CONTENT_LABELS = {"body": "body", "text": "text", "content_md": "content", "content": "content",
                   "description": "description", "subject": "subject", "title": "title", "rows": "rows"}


def _source_kind(untrusted_source: str) -> str:
    s = untrusted_source.lower()
    return "email" if "email" in s else "meeting" if "meeting" in s or "transcript" in s else "external"


def _taint_findings(tainted: bool, control_tainted: bool, tainted_args: Sequence[str] | None,
                    ctl_args: Sequence[str], injection: float, untrusted_source: str) -> tuple[list[str], list[str]]:
    """Say which kind of taint this is. Returns (control notes, content notes): control taint decides
    where the action goes and can force ASK; content taint is informational when the check is clean."""
    if not tainted and not control_tainted:
        return [], []
    if tainted_args is None:
        if control_tainted:
            note = (f"Built from {untrusted_source}; which arguments came from it is unknown, so recipients and "
                    "targets are treated as untrusted")
            return [note], []
        return [], []
    labels: list[str] = []
    for a in ctl_args:
        label = _CONTROL_LABELS.get(a, f"Target {a}")
        if label not in labels:
            labels.append(label)
    control = [f"{label} came from {untrusted_source}, not from you or your contacts" for label in labels]
    content_args = list(dict.fromkeys(_CONTENT_LABELS.get(a, a.replace("_", " "))
                                      for a in tainted_args if not is_control_arg(a)))
    content: list[str] = []
    if content_args:
        verdict = "clean" if injection < THRESHOLDS["reason_injection"] else "flagged"
        what = _join(content_args)
        verb = "draws" if len(content_args) == 1 else "draw"
        content.append(f"{what[0].upper()}{what[1:]} {verb} on untrusted {_source_kind(untrusted_source)} content; "
                       f"injection check {_pct(injection)} ({verdict})")
    if not control and not content:
        content.append(f"Planned after reading {untrusted_source}; no argument values come from it")
    return control, content


def compute_risk(signals: dict[str, float], effect: EffectClass, *, tainted: bool, external: bool,
                 unknown_recipient: bool) -> tuple[float, dict[str, float]]:
    """Weighted sum of penalties, clipped to [0, 1]. Returns (risk, per-term contributions)."""
    comm = effect == EffectClass.COMMUNICATE
    penalties = {
        "misalignment": 1.0 - signals.get("alignment", 1.0),
        "injection": signals.get("injection", 0.0),
        "sensitive": signals.get("sensitive", 0.0),
        "tone": (1.0 - signals.get("tone_ok", 1.0)) if comm else 0.0,
        "recipient_mismatch": (1.0 - signals.get("recipients_match", 1.0)) if comm else 0.0,
        "external": 1.0 if external else 0.0,
        "unknown_recipient": 1.0 if unknown_recipient else 0.0,
        "tainted": 1.0 if tainted else 0.0,
    }
    terms = {k: round(WEIGHTS[k] * max(0.0, min(1.0, v)), 4) for k, v in penalties.items()}
    return round(min(1.0, sum(terms.values())), 4), terms


def decide_gate(
    effect_class: EffectClass | str,
    signals: dict[str, float],
    autonomy: Autonomy | str,
    tainted: bool,
    external: bool,
    unknown_recipient: bool,
    *,
    external_recipients: Sequence[str] = (),  # for specific reasons ("x@y.co is outside acme.dev")
    unknown_recipients: Sequence[str] = (),
    internal_domain: str = "",
    untrusted_source: str = "untrusted content the agent read",  # "an inbound email", ...
    degraded: bool = False,  # signals came from the offline fallback model (Laya)
    tainted_args: Sequence[str] | None = None,  # per-arg provenance; None = unknown (old callers)
    control_tainted: bool | None = None,  # default: derived from tainted_args, or = tainted when unknown
) -> tuple[Verdict, float, list[str]]:
    """Apply SPEC section 6 to one proposed action. Returns (verdict, risk, reasons).

    Reasons are user-facing (approval UI). Rules that decided the verdict come first.
    """
    effect = EffectClass(effect_class)
    level = Autonomy(autonomy)
    if effect == EffectClass.READ:
        return Verdict.AUTO, 0.0, ["Read-only: no side effects"]
    tainted = bool(tainted or tainted_args)
    ctl_args = control_args(tainted_args) if tainted_args is not None else []
    if control_tainted is None:
        control_tainted = bool(ctl_args) if tainted_args is not None else tainted

    comm = effect == EffectClass.COMMUNICATE
    risk, _terms = compute_risk(signals, effect, tainted=tainted, external=external,
                                unknown_recipient=unknown_recipient)
    t = THRESHOLDS
    alignment = signals.get("alignment", 1.0)
    injection = signals.get("injection", 0.0)
    sensitive = signals.get("sensitive", 0.0)
    tone_ok = signals.get("tone_ok", 1.0)
    recipients_match = signals.get("recipients_match", 1.0)
    domain = internal_domain or "your organization"

    # Signal findings, phrased for the user: injection, then control taint, then the rest, then
    # (informational) content taint.
    findings: list[str] = []
    if tainted and injection >= t["reason_injection"]:
        findings.append(f"{_pct(injection)} likely this follows an instruction found in {untrusted_source}, "
                        "not your request")
    elif injection >= t["reason_injection"]:
        findings.append(f"{_pct(injection)} likely this follows an instruction from content you did not write")
    control_notes, content_notes = _taint_findings(tainted, control_tainted, tainted_args, ctl_args, injection,
                                                   untrusted_source)
    findings += control_notes
    if sensitive >= t["reason_sensitive"]:
        where = " to a recipient outside " + domain if external else ""
        findings.append(f"{_pct(sensitive)} likely to disclose confidential material "
                        f"(payment details, credentials, salaries, legal/HR or personal data){where}")
    if external:
        ext = list(external_recipients)
        both = [r for r in ext if r in set(unknown_recipients)]
        if ext and len(both) == len(ext):
            noun = "Recipient" if len(ext) == 1 else "Recipients"
            findings.append(f"{noun} {_join(ext)} {'is' if len(ext) == 1 else 'are'} outside {domain} "
                            "and not a known contact")
        elif ext:
            noun = "Recipient" if len(ext) == 1 else "Recipients"
            findings.append(f"{noun} {_join(ext)} {'is' if len(ext) == 1 else 'are'} outside {domain}")
        else:
            findings.append(f"Goes to someone outside {domain}")
    if unknown_recipient:
        internal_unknown = [r for r in unknown_recipients if r not in set(external_recipients)]
        if internal_unknown:
            noun = "Recipient" if len(internal_unknown) == 1 else "Recipients"
            findings.append(f"{noun} {_join(internal_unknown)} {'is' if len(internal_unknown) == 1 else 'are'} "
                            "not a known contact")
        elif not unknown_recipients:
            findings.append("Includes a recipient who is not a known contact")
    if alignment < t["reason_alignment"]:
        if alignment < 0.34:
            findings.append(f"Not something your request asked for (alignment {alignment:.2f})")
        else:
            findings.append(f"Only loosely related to your request (alignment {alignment:.2f})")
    if comm and tone_ok < t["reason_tone"]:
        findings.append(f"Tone may be inappropriate for the recipients ({_pct(1 - tone_ok)} likely unprofessional)")
    if comm and recipients_match < t["reason_recipients"]:
        findings.append(f"Your request does not clearly name these recipients ({_pct(recipients_match)} match)")
    findings += content_notes

    # --- Always rules --------------------------------------------------------------
    if injection >= t["block_injection"] and tainted:
        return Verdict.BLOCK, risk, [
            f"Blocked: {_pct(injection)} likely this follows an instruction found in {untrusted_source}, not your request",
            *[f for f in findings if not f.startswith(f"{_pct(injection)} likely this follows")],
        ]
    if sensitive >= t["block_sensitive"] and external:
        return Verdict.BLOCK, risk, [
            f"Blocked: {_pct(sensitive)} likely to disclose confidential material to a recipient outside {domain}",
            *[f for f in findings if "confidential material" not in f],
        ]
    if effect == EffectClass.WRITE_IRREVERSIBLE:
        return Verdict.ASK, risk, ["Cannot be undone: irreversible actions always need your approval", *findings]

    # --- Per-autonomy rules ---------------------------------------------------------
    if level == Autonomy.CAUTIOUS:
        return Verdict.ASK, risk, ["Cautious mode: every change needs your approval", *findings]

    if degraded and comm:
        return Verdict.ASK, risk, [
            "Judged by the offline fallback model: messages to people always need your approval", *findings]
    if degraded and tainted:
        return Verdict.ASK, risk, [
            "Judged by the offline fallback model: content from untrusted sources always needs your approval",
            *findings]

    if level == Autonomy.BALANCED:
        if effect == EffectClass.WRITE_REVERSIBLE:
            if control_tainted:
                return Verdict.ASK, risk, ["Where this writes comes from untrusted content: needs your approval",
                                           *findings]
            if tainted and injection >= t["balanced_content_taint_max_injection"]:
                headline = f"Built from untrusted content and the injection check is {_pct(injection)}: needs your approval"
                return Verdict.ASK, risk, [headline, *findings]
            if risk >= t["balanced_reversible_max_risk"]:
                limit = t["balanced_reversible_max_risk"]
                return Verdict.ASK, risk, [f"Risk {risk:.2f} is above the auto-approve limit {limit:.2f}", *findings]
            return Verdict.AUTO, risk, [f"Low risk ({risk:.2f}) and reversible: can be undone from the ledger",
                                        *findings]
        # communicate
        blockers: list[str] = []
        if control_tainted:  # content taint alone is allowed when the injection check is clean
            blockers.append("has recipients or a target taken from untrusted content")
        if external:
            blockers.append("goes outside " + domain)
        if unknown_recipient:
            blockers.append("includes a recipient who is not a known contact")
        if alignment < t["balanced_comm_min_alignment"]:
            blockers.append("is not clearly what you asked for")
        if injection >= t["balanced_comm_max_injection"]:
            blockers.append("may follow an instruction from untrusted content")
        if sensitive >= t["balanced_comm_max_sensitive"]:
            blockers.append("may contain confidential material")
        if tone_ok < t["balanced_comm_min_tone"]:
            blockers.append("may have an inappropriate tone")
        if blockers:
            return Verdict.ASK, risk, [f"Messages cannot be unsent, and this one {_join(blockers, 6)}", *findings]
        return Verdict.AUTO, risk, [
            f"Internal message to known colleagues that matches your request (risk {risk:.2f})", *findings]

    # autonomous
    if tainted and injection >= t["autonomous_tainted_max_injection"]:
        return Verdict.ASK, risk, [
            f"Built from untrusted content and {_pct(injection)} likely to follow an instruction in it", *findings]
    if control_tainted and injection >= t["autonomous_control_taint_max_injection"]:
        return Verdict.ASK, risk, [
            f"Recipients or target come from untrusted content and the injection check is {_pct(injection)}",
            *findings]
    if risk >= t["autonomous_max_risk"]:
        return Verdict.ASK, risk, [f"Risk {risk:.2f} is above the autonomous limit {t['autonomous_max_risk']:.2f}",
                                   *findings]
    return Verdict.AUTO, risk, [f"Risk {risk:.2f} is below the autonomous limit {t['autonomous_max_risk']:.2f}",
                                *findings]


def fallback_gate(effect_class: EffectClass | str) -> tuple[Verdict, float, list[str]]:
    """Judge unavailable: never AUTO a non-read action."""
    if EffectClass(effect_class) == EffectClass.READ:
        return Verdict.AUTO, 0.0, ["Read-only: no side effects"]
    return Verdict.ASK, 1.0, ["judge unavailable, failing safe"]


# ---------------------------------------------------------------------------
# Failure recovery (SPEC section 3)
# ---------------------------------------------------------------------------


def recovery_strategy(
    cause: ErrorKind | str,
    confidence: float,
    attempts: int,  # attempts made so far, including the one that just failed
    optional: bool,
    alternatives: Iterable[str] = (),
) -> tuple[RecoveryStrategy, str]:
    """Map a classified failure to a recovery strategy. Returns (strategy, note).

    Order: a retryable transient error retries (bounded) -> an optional step is skipped ->
    low classifier confidence asks a human -> per-cause mapping.
    """
    kind = ErrorKind(cause)
    alts = [a for a in alternatives if a]
    max_transient = int(THRESHOLDS["max_transient_attempts"])
    sure = confidence >= THRESHOLDS["recovery_min_confidence"]

    if kind == ErrorKind.TRANSIENT and sure and attempts < max_transient:
        return RecoveryStrategy.RETRY_SAME, f"Temporary failure: retry with backoff (attempt {attempts + 1} of {max_transient})"
    if optional:
        return RecoveryStrategy.SKIP, "Optional step: continue without it"
    if not sure:
        return RecoveryStrategy.ASK_HUMAN, f"Cause unclear (confidence {confidence:.2f}): asking you how to proceed"
    if kind == ErrorKind.TRANSIENT:
        return RecoveryStrategy.ASK_HUMAN, f"Still failing after {attempts} attempts: asking you how to proceed"
    if kind == ErrorKind.INVALID_ARGS:
        if attempts >= int(THRESHOLDS["max_repair_attempts"]):
            return RecoveryStrategy.ASK_HUMAN, f"Arguments still invalid after {attempts} attempts"
        return RecoveryStrategy.REPAIR_ARGS, "Arguments rejected: repair them from the error and the tool schema"
    if kind in (ErrorKind.NOT_FOUND, ErrorKind.PRECONDITION):
        if alts:
            return RecoveryStrategy.SWITCH_TOOL, f"Try {alts[0]} instead"
        what = "Referenced object does not exist" if kind == ErrorKind.NOT_FOUND else "The world state prevents this step"
        return RecoveryStrategy.REPLAN, f"{what}: re-plan the parent goal"
    if kind in (ErrorKind.AUTH, ErrorKind.PERMISSION):
        what = "Credentials are missing or expired" if kind == ErrorKind.AUTH else "Not permitted"
        return RecoveryStrategy.ASK_HUMAN, f"{what}: needs you"
    return RecoveryStrategy.ASK_HUMAN, "Unrecognized failure: asking you how to proceed"


# ---------------------------------------------------------------------------
# Export for GET /api/config
# ---------------------------------------------------------------------------


def describe() -> dict[str, Any]:
    return {
        "version": POLICY_VERSION,
        "weights": dict(WEIGHTS),
        "thresholds": dict(THRESHOLDS),
        "rules": list(RULES),
        "control_args": sorted(CONTROL_ARGS) + ["*_id"],
        "risk": "risk = min(1, sum(weight * penalty)); penalties: misalignment = 1 - alignment, "
        "tone = 1 - tone_ok and recipient_mismatch = 1 - recipients_match (communications only), "
        "injection, sensitive, and external / unknown_recipient / tainted as 0 or 1",
        "signals": {
            "alignment": "Jev Score 0..3, normalized to 0..1: how directly the action serves your request",
            "injection": "Jev Noul: the action carries out an instruction found in untrusted content",
            "sensitive": "Jev Noul: the outgoing content discloses confidential material",
            "tone_ok": "Jev Noul (communications): professional and appropriate for the recipients",
            "recipients_match": "Jev Noul (communications): your request implies these recipients",
            "external": "code: any recipient outside the internal domain",
            "unknown_recipient": "code: any recipient not among known contacts",
            "tainted": "code: arguments derive from untrusted content",
            "control_tainted": "code: a control argument (recipients, channel, url, parent, *_id target) derives "
            "from untrusted content",
        },
    }
