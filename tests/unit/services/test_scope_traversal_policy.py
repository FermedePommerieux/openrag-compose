"""Pure matrix tests for the versioned documentary scope policy."""

import pytest

from services.scope_traversal_policy import (
    SCOPE_POLICY_ID,
    SCOPE_POLICY_VERSION,
    ScopeRelationSemantics,
    ScopeTraversalPolicy,
)


@pytest.fixture
def policy() -> ScopeTraversalPolicy:
    return ScopeTraversalPolicy()


@pytest.mark.parametrize(
    (
        "role",
        "source_type",
        "target_type",
        "forward",
        "reverse",
        "transitive",
        "semantics",
        "certifiable",
    ),
    [
        (
            "attachment_of",
            "email_attachment",
            "email_message",
            True,
            True,
            "controlled",
            ScopeRelationSemantics.SCOPE_DEFINING,
            True,
        ),
        (
            "member_of",
            "email_message",
            "email_thread",
            True,
            True,
            True,
            ScopeRelationSemantics.SCOPE_DEFINING,
            True,
        ),
        (
            "member_of",
            "email_attachment",
            "email_thread",
            True,
            True,
            True,
            ScopeRelationSemantics.SCOPE_DEFINING,
            True,
        ),
        (
            "member_of",
            "file",
            "directory_collection",
            False,
            False,
            False,
            ScopeRelationSemantics.INFRASTRUCTURE,
            True,
        ),
        (
            "contained_in",
            "email_message",
            "email_archive",
            False,
            False,
            False,
            ScopeRelationSemantics.CONTEXTUAL,
            True,
        ),
        (
            "reply_to",
            "email_message",
            "email_message",
            True,
            True,
            True,
            ScopeRelationSemantics.SCOPE_DEFINING,
            True,
        ),
        (
            "reply_to",
            "email_message",
            "email_message_identifier",
            True,
            True,
            True,
            ScopeRelationSemantics.SCOPE_DEFINING,
            True,
        ),
        (
            "references",
            "email_message",
            "email_message_identifier",
            True,
            True,
            True,
            ScopeRelationSemantics.SCOPE_DEFINING,
            True,
        ),
        (
            "unknown_role",
            "email_message",
            "email_message",
            False,
            False,
            False,
            ScopeRelationSemantics.UNCLASSIFIED,
            False,
        ),
        (
            "member_of",
            "email_message",
            "unknown_target",
            False,
            False,
            False,
            ScopeRelationSemantics.UNCLASSIFIED,
            False,
        ),
        (
            "reply_to",
            "unknown_source",
            "email_message",
            False,
            False,
            False,
            ScopeRelationSemantics.UNCLASSIFIED,
            False,
        ),
        (
            "references",
            "email_message",
            "",
            False,
            False,
            False,
            ScopeRelationSemantics.UNCLASSIFIED,
            False,
        ),
    ],
)
def test_scope_policy_matrix(
    policy,
    role,
    source_type,
    target_type,
    forward,
    reverse,
    transitive,
    semantics,
    certifiable,
):
    decision = policy.classify(
        role=role,
        source_type=source_type,
        target_type=target_type,
    )

    assert decision.follow_forward is forward
    assert decision.follow_reverse is reverse
    assert decision.transitive == transitive
    assert decision.semantics is semantics
    assert decision.certifiable is certifiable
    assert decision.follows("forward") is forward
    assert decision.follows("reverse") is reverse


def test_scope_policy_identity_is_stable_and_versioned(policy):
    assert policy.policy_id == SCOPE_POLICY_ID == "documentary-prov-o"
    assert policy.version == SCOPE_POLICY_VERSION == 1
    assert policy.rules == ScopeTraversalPolicy().rules


def test_reverse_rules_never_include_context_or_infrastructure(policy):
    assert policy.reverse_rules_for_target("email_archive") == ()
    assert policy.reverse_rules_for_target("directory_collection") == ()
    assert {
        (rule.role, rule.source_type, rule.target_type)
        for rule in policy.reverse_rules_for_target("email_thread")
    } == {
        ("member_of", "email_message", "email_thread"),
        ("member_of", "email_attachment", "email_thread"),
    }
