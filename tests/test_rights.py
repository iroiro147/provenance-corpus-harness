import pytest

from harness.rights import AuthorizationDeclaration, RightsDeclaration, SourcePolicy


def test_public_policy_round_trips() -> None:
    policy = SourcePolicy(
        RightsDeclaration("public", ("local-research", "redistribution")),
        AuthorizationDeclaration("public"),
        "public",
        False,
    )
    assert SourcePolicy.from_mapping(policy.to_dict()) == policy


@pytest.mark.parametrize("access_class", ["paid-personal", "private", "unknown"])
def test_restricted_classes_must_remain_local(access_class: str) -> None:
    with pytest.raises(ValueError, match="local_only"):
        SourcePolicy(
            RightsDeclaration(access_class),  # type: ignore[arg-type]
            AuthorizationDeclaration("account-owned-export", account_owner="operator"),
            access_class,  # type: ignore[arg-type]
            False,
        )


def test_authorization_cross_field_requirements() -> None:
    with pytest.raises(ValueError, match="approval_id"):
        AuthorizationDeclaration("operator-approved-export")
    with pytest.raises(ValueError, match="account_owner"):
        AuthorizationDeclaration("account-owned-export")
    with pytest.raises(ValueError, match="licensed rights"):
        SourcePolicy(
            RightsDeclaration("owned"),
            AuthorizationDeclaration("licensed-dataset"),
            "owned",
        )


def test_rights_reject_duplicate_or_empty_uses() -> None:
    with pytest.raises(ValueError):
        RightsDeclaration("public", ())
    with pytest.raises(ValueError):
        RightsDeclaration("public", ("local-research", "local-research"))


def test_rights_status_must_match_access_class() -> None:
    with pytest.raises(ValueError, match="must match"):
        SourcePolicy(
            RightsDeclaration("private"),
            AuthorizationDeclaration("official-api"),
            "public",
            False,
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: RightsDeclaration("public", notes="sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"),
        lambda: RightsDeclaration("public", ("Authorization: Bearer abcdefghijklmnop",)),
        lambda: AuthorizationDeclaration(
            "account-owned-export", account_owner="https://example.com/?access_token=secret"
        ),
        lambda: AuthorizationDeclaration(
            "operator-approved-export", approval_id="Cookie: session=secret"
        ),
        lambda: RightsDeclaration(
            "public", notes="reference https://example.com/?access_token=secret"
        ),
    ],
)
def test_policy_strings_reject_credential_material(factory) -> None:
    with pytest.raises(ValueError, match="credential"):
        factory()
