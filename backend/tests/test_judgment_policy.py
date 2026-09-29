"""Unit tests for app/judgment/policy.py (pure code, no network)."""
from __future__ import annotations

import pytest

from app.core.models import Autonomy, EffectClass, ErrorKind, RecoveryStrategy, Verdict
from app.judgment import policy
from app.judgment.policy import THRESHOLDS, WEIGHTS, compute_risk, decide_gate, describe, recovery_strategy

W, C, I, R = (EffectClass.WRITE_REVERSIBLE, EffectClass.COMMUNICATE, EffectClass.WRITE_IRREVERSIBLE,
              EffectClass.READ)

CLEAN_COMM = {"alignment": 1.0, "injection": 0.0, "sensitive": 0.05, "tone_ok": 0.97, "recipients_match": 0.95}
CLEAN_WRITE = {"alignment": 1.0, "injection": 0.0, "sensitive": 0.05}


def gate(effect, signals, autonomy="balanced", tainted=False, external=False, unknown=False, **kw):
    return decide_gate(effect, signals, Autonomy(autonomy), tainted, external, unknown, **kw)


# --- risk ---------------------------------------------------------------------------


def test_risk_is_weighted_sum_of_penalties():
    sig = {"alignment": 0.8, "injection": 0.1, "sensitive": 0.1, "tone_ok": 0.9, "recipients_match": 0.5}
    risk, terms = compute_risk(sig, C, tainted=True, external=True, unknown_recipient=False)
    expected = (WEIGHTS["misalignment"] * 0.2 + WEIGHTS["injection"] * 0.1 + WEIGHTS["sensitive"] * 0.1
                + WEIGHTS["tone"] * 0.1 + WEIGHTS["recipient_mismatch"] * 0.5 + WEIGHTS["external"]
                + WEIGHTS["tainted"])
    assert expected < 1.0
    assert risk == pytest.approx(expected, abs=1e-3)
    assert terms["unknown_recipient"] == 0.0


def test_risk_ignores_communication_terms_for_writes_and_clips():
    risk, terms = compute_risk({"alignment": 1.0, "tone_ok": 0.0, "recipients_match": 0.0}, W,
                               tainted=False, external=False, unknown_recipient=False)
    assert risk == 0.0 and terms["tone"] == 0.0 and terms["recipient_mismatch"] == 0.0
    worst, _ = compute_risk({"alignment": 0.0, "injection": 1.0, "sensitive": 1.0, "tone_ok": 0.0}, C,
                            tainted=True, external=True, unknown_recipient=True)
    assert worst == 1.0


# --- always rules ---------------------------------------------------------------------


@pytest.mark.parametrize("autonomy", ["cautious", "balanced", "autonomous"])
def test_read_is_always_auto(autonomy):
    verdict, risk, reasons = gate(R, {"injection": 1.0, "sensitive": 1.0}, autonomy, tainted=True, external=True)
    assert verdict == Verdict.AUTO and risk == 0.0 and reasons


@pytest.mark.parametrize("autonomy", ["cautious", "balanced", "autonomous"])
def test_tainted_injection_blocks_in_every_mode(autonomy):
    sig = {**CLEAN_COMM, "injection": 0.7}
    verdict, _, reasons = gate(C, sig, autonomy, tainted=True)
    assert verdict == Verdict.BLOCK
    assert reasons[0].startswith("Blocked: 70% likely this follows an instruction")


def test_injection_without_taint_does_not_block():
    verdict, _, _ = gate(C, {**CLEAN_COMM, "injection": 0.9}, "balanced", tainted=False)
    assert verdict == Verdict.ASK


@pytest.mark.parametrize("autonomy", ["cautious", "balanced", "autonomous"])
def test_sensitive_external_blocks(autonomy):
    verdict, _, reasons = gate(C, {**CLEAN_COMM, "sensitive": 0.85}, autonomy, external=True,
                               external_recipients=["x@evil.co"], internal_domain="acme.dev")
    assert verdict == Verdict.BLOCK
    assert "outside acme.dev" in reasons[0]


def test_sensitive_internal_asks_not_blocks():
    verdict, _, _ = gate(C, {**CLEAN_COMM, "sensitive": 0.95}, "balanced")
    assert verdict == Verdict.ASK
    verdict, _, _ = gate(C, {**CLEAN_COMM, "sensitive": 0.95}, "autonomous")
    assert verdict == Verdict.ASK  # sensitive alone exceeds the autonomous risk limit


@pytest.mark.parametrize("autonomy", ["balanced", "autonomous"])
def test_irreversible_never_auto(autonomy):
    verdict, _, reasons = gate(I, CLEAN_WRITE, autonomy)
    assert verdict == Verdict.ASK
    assert "Cannot be undone" in reasons[0]


# --- cautious ---------------------------------------------------------------------------


@pytest.mark.parametrize("effect", [W, C, I])
def test_cautious_asks_for_every_write(effect):
    verdict, _, reasons = gate(effect, CLEAN_COMM, "cautious")
    assert verdict == Verdict.ASK and reasons


# --- balanced ---------------------------------------------------------------------------


def test_balanced_reversible_low_risk_auto():
    verdict, risk, _ = gate(W, CLEAN_WRITE, "balanced")
    assert verdict == Verdict.AUTO and risk < THRESHOLDS["balanced_reversible_max_risk"]


def test_balanced_reversible_tainted_asks():
    verdict, _, reasons = gate(W, CLEAN_WRITE, "balanced", tainted=True)
    assert verdict == Verdict.ASK and "untrusted" in reasons[0]


def test_balanced_reversible_high_risk_asks():
    verdict, risk, reasons = gate(W, {**CLEAN_WRITE, "alignment": 0.33}, "balanced")
    assert verdict == Verdict.ASK and risk >= THRESHOLDS["balanced_reversible_max_risk"]
    assert any("alignment" in r for r in reasons)


def test_balanced_comm_clean_internal_auto():
    verdict, _, reasons = gate(C, CLEAN_COMM, "balanced")
    assert verdict == Verdict.AUTO
    assert "Internal message" in reasons[0]


@pytest.mark.parametrize("change,flag", [
    ({"alignment": 0.6}, None),
    ({"injection": 0.25}, None),
    ({"sensitive": 0.35}, None),
    ({"tone_ok": 0.6}, None),
    ({}, "external"),
    ({}, "unknown"),
    ({}, "tainted"),  # tainted with unknown per-arg provenance counts as control taint
])
def test_balanced_comm_each_condition_forces_ask(change, flag):
    kwargs = {flag: True} if flag else {}
    verdict, _, _ = gate(C, {**CLEAN_COMM, **change}, "balanced", **kwargs)
    assert verdict == Verdict.ASK


def test_balanced_comm_boundaries_are_inclusive_as_specified():
    t = THRESHOLDS
    at_edge = {**CLEAN_COMM, "alignment": t["balanced_comm_min_alignment"], "tone_ok": t["balanced_comm_min_tone"],
               "injection": t["balanced_comm_max_injection"] - 0.01, "sensitive": t["balanced_comm_max_sensitive"] - 0.01}
    assert gate(C, at_edge, "balanced")[0] == Verdict.AUTO
    assert gate(C, {**at_edge, "injection": t["balanced_comm_max_injection"]}, "balanced")[0] == Verdict.ASK


# --- autonomous -------------------------------------------------------------------------


def test_autonomous_external_known_low_risk_auto():
    verdict, risk, _ = gate(C, CLEAN_COMM, "autonomous", tainted=True, external=True)
    assert verdict == Verdict.AUTO and risk < THRESHOLDS["autonomous_max_risk"]


def test_autonomous_tainted_moderate_injection_asks():
    verdict, _, reasons = gate(C, {**CLEAN_COMM, "injection": 0.35}, "autonomous", tainted=True)
    assert verdict == Verdict.ASK and "untrusted content" in reasons[0]


@pytest.mark.parametrize("change", [{"tone_ok": 0.1}, {"alignment": 0.1}])
def test_autonomous_single_serious_problem_asks(change):
    verdict, risk, _ = gate(C, {**CLEAN_COMM, **change}, "autonomous")
    assert verdict == Verdict.ASK and risk >= THRESHOLDS["autonomous_max_risk"]


def test_degraded_model_never_autos_communication():
    verdict, _, reasons = gate(C, CLEAN_COMM, "autonomous", degraded=True)
    assert verdict == Verdict.ASK and "fallback" in reasons[0]
    assert gate(W, CLEAN_WRITE, "balanced", degraded=True)[0] == Verdict.AUTO
    assert gate(W, CLEAN_WRITE, "autonomous", tainted=True, degraded=True)[0] == Verdict.ASK


# --- reasons ----------------------------------------------------------------------------


def test_reasons_are_specific():
    _, _, reasons = gate(
        C, {**CLEAN_COMM, "injection": 0.87, "recipients_match": 0.1}, "balanced", tainted=True, external=True,
        unknown=True, external_recipients=["billing-update@globex-payments.co"],
        unknown_recipients=["billing-update@globex-payments.co"], internal_domain="acme.dev",
        untrusted_source="an inbound email")
    text = "\n".join(reasons)
    assert "87% likely this follows an instruction found in an inbound email, not your request" in text
    assert "Recipient billing-update@globex-payments.co is outside acme.dev and not a known contact" in text


def test_unknown_internal_recipient_reason():
    verdict, _, reasons = gate(C, CLEAN_COMM, "balanced", unknown=True, unknown_recipients=["new.hire@acme.dev"],
                               internal_domain="acme.dev")
    assert verdict == Verdict.ASK
    assert any("new.hire@acme.dev is not a known contact" in r for r in reasons)


def test_fallback_gate():
    assert policy.fallback_gate(C) == (Verdict.ASK, 1.0, ["judge unavailable, failing safe"])
    assert policy.fallback_gate(R)[0] == Verdict.AUTO


# --- recovery ---------------------------------------------------------------------------


@pytest.mark.parametrize("cause,conf,attempts,optional,alts,expected", [
    (ErrorKind.TRANSIENT, 0.9, 1, False, [], RecoveryStrategy.RETRY_SAME),
    (ErrorKind.TRANSIENT, 0.9, 2, False, [], RecoveryStrategy.RETRY_SAME),
    (ErrorKind.TRANSIENT, 0.9, 3, False, [], RecoveryStrategy.ASK_HUMAN),  # max 3 attempts
    (ErrorKind.TRANSIENT, 0.9, 3, True, [], RecoveryStrategy.SKIP),
    (ErrorKind.INVALID_ARGS, 0.9, 1, False, [], RecoveryStrategy.REPAIR_ARGS),
    (ErrorKind.INVALID_ARGS, 0.9, 3, False, [], RecoveryStrategy.ASK_HUMAN),
    (ErrorKind.NOT_FOUND, 0.9, 1, False, [], RecoveryStrategy.REPLAN),
    (ErrorKind.NOT_FOUND, 0.9, 1, False, ["docs.search"], RecoveryStrategy.SWITCH_TOOL),
    (ErrorKind.PRECONDITION, 0.9, 1, False, [], RecoveryStrategy.REPLAN),
    (ErrorKind.AUTH, 0.9, 1, False, [], RecoveryStrategy.ASK_HUMAN),
    (ErrorKind.PERMISSION, 0.9, 1, False, [], RecoveryStrategy.ASK_HUMAN),
    (ErrorKind.UNKNOWN, 0.9, 1, False, [], RecoveryStrategy.ASK_HUMAN),
    (ErrorKind.INVALID_ARGS, 0.9, 1, True, [], RecoveryStrategy.SKIP),
    (ErrorKind.INVALID_ARGS, 0.4, 1, False, [], RecoveryStrategy.ASK_HUMAN),  # low confidence
    (ErrorKind.TRANSIENT, 0.4, 1, False, [], RecoveryStrategy.ASK_HUMAN),
])
def test_recovery_strategy(cause, conf, attempts, optional, alts, expected):
    strategy, note = recovery_strategy(cause, conf, attempts, optional, alts)
    assert strategy == expected
    assert note


def test_switch_tool_note_names_alternative():
    strategy, note = recovery_strategy(ErrorKind.NOT_FOUND, 0.9, 1, False, ["docs.search", "notion.search"])
    assert strategy == RecoveryStrategy.SWITCH_TOOL and "docs.search" in note


# --- config -----------------------------------------------------------------------------


def test_describe_exports_policy():
    d = describe()
    assert d["weights"] == WEIGHTS and d["thresholds"] == THRESHOLDS
    assert d["rules"] and set(d["signals"]) >= {"alignment", "injection", "sensitive", "tone_ok", "external",
                                                 "unknown_recipient", "tainted"}
    assert THRESHOLDS["block_injection"] == 0.7 and THRESHOLDS["block_sensitive"] == 0.8
    assert THRESHOLDS["balanced_reversible_max_risk"] == 0.35 and THRESHOLDS["autonomous_max_risk"] == 0.6


# --- per-argument taint ------------------------------------------------------------------


def test_control_args():
    assert policy.control_args(["body", "to", "doc_id", "subject", "reply_to_id", "channel"]) == [
        "to", "doc_id", "reply_to_id", "channel"]
    assert policy.is_control_arg("sheet_id") and policy.is_control_arg("parent") and not policy.is_control_arg("body")


def test_content_tainted_notion_page_autos_in_balanced():
    verdict, _, reasons = gate(W, {**CLEAN_WRITE, "injection": 0.03}, "balanced", tainted=True,
                               tainted_args=["content_md"], untrusted_source="a meeting transcript")
    assert verdict == Verdict.AUTO
    assert "Content draws on untrusted meeting content; injection check 3% (clean)" in reasons


@pytest.mark.parametrize("effect", [W, C])
def test_content_taint_with_moderate_injection_asks(effect):
    verdict, _, _ = gate(effect, {**CLEAN_COMM, "injection": 0.5}, "balanced", tainted=True, tainted_args=["body"])
    assert verdict == Verdict.ASK
    assert gate(effect, {**CLEAN_COMM, "injection": 0.5}, "autonomous", tainted=True, tainted_args=["body"])[0] \
        == Verdict.ASK


def test_content_tainted_internal_email_autos_in_balanced():
    verdict, _, reasons = gate(C, {**CLEAN_COMM, "injection": 0.03}, "balanced", tainted=True, tainted_args=["body"],
                               untrusted_source="a meeting transcript")
    assert verdict == Verdict.AUTO
    assert any("injection check 3% (clean)" in r for r in reasons)


def test_control_tainted_recipients_ask_in_balanced():
    verdict, _, reasons = gate(C, CLEAN_COMM, "balanced", tainted=True, tainted_args=["to", "body"],
                               untrusted_source="an inbound email")
    assert verdict == Verdict.ASK
    assert "recipients or a target taken from untrusted content" in reasons[0]
    assert "Recipient list came from an inbound email, not from you or your contacts" in reasons


def test_control_tainted_target_id_asks_for_reversible_write():
    verdict, _, reasons = gate(W, CLEAN_WRITE, "balanced", tainted=True, tainted_args=["doc_id", "content_md"],
                               untrusted_source="an inbound email")
    assert verdict == Verdict.ASK
    assert "Target document came from an inbound email, not from you or your contacts" in reasons


def test_autonomous_control_taint_rule():
    base = {**CLEAN_COMM, "injection": 0.25}
    assert gate(C, base, "autonomous", tainted=True, tainted_args=["to"])[0] == Verdict.ASK
    assert gate(C, base, "autonomous", tainted=True, tainted_args=["body"])[0] == Verdict.AUTO  # 0.25 < 0.30
    assert gate(C, {**CLEAN_COMM, "injection": 0.05}, "autonomous", tainted=True, tainted_args=["to"])[0] == Verdict.AUTO


def test_block_rules_still_apply_to_content_taint():
    verdict, _, _ = gate(C, {**CLEAN_COMM, "injection": 0.9}, "balanced", tainted=True, tainted_args=["body"])
    assert verdict == Verdict.BLOCK


def test_describe_exposes_control_args():
    d = describe()
    assert set(d["control_args"]) >= {"to", "cc", "bcc", "attendees", "channel", "url", "parent", "*_id"}
    assert "control_tainted" in d["signals"]
    assert any("Per-argument taint" in r for r in d["rules"])
