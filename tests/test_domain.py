from uuid import uuid4

import pytest

from floorball_bot.auth import require_city_scope, require_roles
from floorball_bot.domain import (
    Actor,
    DraftStatus,
    Role,
    can_transition,
    normalize_phone,
    verify_self_contact,
)
from floorball_bot.errors import AuthorizationError


def actor(*roles: Role, scopes=()) -> Actor:
    return Actor(
        user_id=uuid4(),
        telegram_id=123,
        roles=frozenset(roles),
        city_scopes=frozenset(scopes),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+7 701 123 45 67", "+77011234567"),
        ("8 (701) 123-45-67", "+77011234567"),
    ],
)
def test_kazakhstan_phone_normalization(raw, expected):
    assert normalize_phone(raw) == expected


def test_invalid_phone_rejected():
    with pytest.raises(ValueError):
        normalize_phone("123")


def test_contact_must_belong_to_sender():
    verify_self_contact(sender_id=10, contact_user_id=10)
    with pytest.raises(ValueError):
        verify_self_contact(sender_id=10, contact_user_id=11)
    with pytest.raises(ValueError):
        verify_self_contact(sender_id=10, contact_user_id=None)


def test_roles_and_city_scopes_are_enforced():
    city_a, city_b = uuid4(), uuid4()
    coach = actor(Role.CITY_COACH, scopes=[city_a])
    require_roles(coach, Role.CITY_COACH)
    require_city_scope(coach, city_a)
    with pytest.raises(AuthorizationError):
        require_city_scope(coach, city_b)
    with pytest.raises(AuthorizationError):
        require_roles(coach, Role.SUPERADMIN)
    require_city_scope(actor(Role.SUPERADMIN), city_b)


def test_draft_state_machine_happy_path_and_forbidden_skip():
    path = [
        DraftStatus.COLLECTING,
        DraftStatus.READY_FOR_USER_REVIEW,
        DraftStatus.SUBMITTED,
        DraftStatus.UNDER_REVIEW,
        DraftStatus.APPROVED,
        DraftStatus.PUBLISHING,
        DraftStatus.PUBLISHED,
    ]
    assert all(can_transition(left, right) for left, right in zip(path, path[1:], strict=False))
    assert not can_transition(DraftStatus.COLLECTING, DraftStatus.APPROVED)
    assert not can_transition(DraftStatus.SUBMITTED, DraftStatus.PUBLISHED)
