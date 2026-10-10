"""表单运行时状态：绑定、发布卡片、分步草稿、正式提交、名额封顶。"""

from __future__ import annotations

import pytest

from bot.modules.forms.store import (
    APPROVED,
    CLOSED,
    NEEDS_INFO,
    OPEN,
    PENDING,
    RECEIVED,
    build_stores,
    new_storage_key,
)
from tests.conftest import GUILD_ID


@pytest.fixture
def stores(db):
    return build_stores(db)


# ---------------------------------------------------------------- 绑定


async def test_binding_without_a_row_is_empty_and_not_persisted(stores) -> None:
    binding = await stores.bindings.get(GUILD_ID, "team_application")

    assert binding.is_configured is False
    row = await stores.bindings.get(GUILD_ID, "team_application")
    assert row.updated_at == ""


async def test_binding_update_merges_and_keeps_other_items(stores) -> None:
    await stores.bindings.update(GUILD_ID, "team_application", review_channel_id=11, grant_role_id=22)

    binding = await stores.bindings.update(GUILD_ID, "team_application", reviewer_role_id=33)

    assert (binding.review_channel_id, binding.reviewer_role_id, binding.grant_role_id) == (11, 33, 22)
    assert binding.is_configured is True
    assert binding.updated_at != ""


async def test_binding_can_be_cleared_by_passing_none(stores) -> None:
    await stores.bindings.update(GUILD_ID, "team_application", grant_role_id=22)

    binding = await stores.bindings.update(GUILD_ID, "team_application", grant_role_id=None)

    assert binding.grant_role_id is None


async def test_binding_update_rejects_unknown_items(stores) -> None:
    with pytest.raises(ValueError):
        await stores.bindings.update(GUILD_ID, "team_application", review_channel=1)


# ---------------------------------------------------------------- 发布


async def create_publication(stores, **overrides):
    params = {
        "form_id": "event_signup",
        "channel_id": 6103,
        "capacity": None,
        "closes_at": None,
        "created_by": 400,
    }
    params.update(overrides)
    return await stores.publications.create(GUILD_ID, **params)


async def test_publication_is_found_by_message_id(stores) -> None:
    publication = await create_publication(stores)
    await stores.publications.set_message(publication.publication_id, 900)

    found = await stores.publications.get_by_message(900)

    assert found is not None
    assert found.publication_id == publication.publication_id
    assert found.status == OPEN
    assert found.remaining is None


async def test_closed_publications_drop_out_of_the_open_list(stores) -> None:
    publication = await create_publication(stores)
    await stores.publications.set_status(publication.publication_id, CLOSED)

    assert await stores.publications.open_in_guild(GUILD_ID) == []
    assert await stores.publications.open_in_guild(GUILD_ID, "event_signup") == []


async def test_due_only_returns_open_publications_with_a_passed_deadline(stores) -> None:
    due = await create_publication(stores, closes_at="2026-10-07T00:00:00+00:00")
    await create_publication(stores, closes_at=None)
    closed = await create_publication(stores, closes_at="2026-10-07T00:00:00+00:00")
    await stores.publications.set_status(closed.publication_id, CLOSED)

    found = await stores.publications.due("2026-10-07T12:00:00+00:00")

    assert [item.publication_id for item in found] == [due.publication_id]


async def test_signup_slots_are_capped_by_one_statement(stores) -> None:
    publication = await create_publication(stores, capacity=2)

    assert await stores.publications.take_slot(publication.publication_id) is True
    assert await stores.publications.take_slot(publication.publication_id) is True
    assert await stores.publications.take_slot(publication.publication_id) is False

    latest = await stores.publications.get(publication.publication_id)
    assert latest is not None and latest.taken == 2 and latest.is_full


async def test_a_taken_slot_can_be_given_back(stores) -> None:
    publication = await create_publication(stores, capacity=1)
    await stores.publications.take_slot(publication.publication_id)

    await stores.publications.give_back_slot(publication.publication_id)

    latest = await stores.publications.get(publication.publication_id)
    assert latest is not None and latest.taken == 0 and latest.remaining == 1


async def test_unlimited_signup_never_runs_out(stores) -> None:
    publication = await create_publication(stores, capacity=None)

    for _ in range(3):
        assert await stores.publications.take_slot(publication.publication_id) is True

    latest = await stores.publications.get(publication.publication_id)
    assert latest is not None and latest.is_full is False and latest.remaining is None


# ---------------------------------------------------------------- 草稿


async def test_draft_round_trip_keeps_its_storage_key(stores) -> None:
    first = await stores.drafts.save(
        GUILD_ID,
        form_id="team_application",
        discord_user_id=300,
        step=1,
        answers={"work": ["test"]},
    )
    second = await stores.drafts.save(
        GUILD_ID,
        form_id="team_application",
        discord_user_id=300,
        step=1,
        answers={"work": ["test"], "experience": "more"},
    )

    assert first.storage_key == second.storage_key
    assert second.answers == {"work": ["test"], "experience": "more"}
    assert await stores.drafts.get(GUILD_ID, "team_application", 300) is not None


async def test_drafts_are_per_user_and_form(stores) -> None:
    await stores.drafts.save(GUILD_ID, form_id="feedback", discord_user_id=300, step=0, answers={})

    assert await stores.drafts.get(GUILD_ID, "feedback", 301) is None
    assert await stores.drafts.get(GUILD_ID, "team_application", 300) is None


async def test_stale_drafts_are_listed_and_deletable(stores, db) -> None:
    draft = await stores.drafts.save(GUILD_ID, form_id="feedback", discord_user_id=300, step=0, answers={})
    await db.execute("UPDATE form_drafts SET updated_at = ?", ("2026-01-01T00:00:00+00:00",))

    stale = await stores.drafts.stale("2026-10-07T00:00:00+00:00")

    assert [item.storage_key for item in stale] == [draft.storage_key]
    await stores.drafts.delete(GUILD_ID, "feedback", 300)
    assert await stores.drafts.get(GUILD_ID, "feedback", 300) is None


def test_storage_keys_are_plain_hex() -> None:
    key = new_storage_key()

    assert len(key) == 32
    assert int(key, 16) >= 0


# ---------------------------------------------------------------- 提交


async def create_submission(stores, **overrides):
    params = {
        "form_id": "team_application",
        "publication_id": 1,
        "discord_user_id": 300,
        "answers": {"work": ["translate"]},
        "status": PENDING,
        "attachment_dir": new_storage_key(),
    }
    params.update(overrides)
    return await stores.submissions.create(GUILD_ID, **params)


async def test_submission_round_trip(stores) -> None:
    submission = await create_submission(stores)

    found = await stores.submissions.get(submission.submission_id)

    assert found is not None
    assert found.answers == {"work": ["translate"]}
    assert found.status == PENDING
    assert found.is_pending is True
    assert await stores.submissions.get_by_dir(submission.attachment_dir) is not None


async def test_submission_is_found_by_review_message_and_latest_for_user(stores) -> None:
    submission = await create_submission(stores)
    await stores.submissions.set_review_message(submission.submission_id, channel_id=6101, message_id=777)

    by_message = await stores.submissions.get_by_review_message(777)
    latest = await stores.submissions.latest_for_user(GUILD_ID, "team_application", 300)

    assert by_message is not None and by_message.submission_id == submission.submission_id
    assert by_message.review_channel_id == 6101
    assert latest is not None and latest.submission_id == submission.submission_id
    assert await stores.submissions.latest_for_user(GUILD_ID, "team_application", 999) is None


async def test_replace_answers_clears_the_previous_decision(stores) -> None:
    submission = await create_submission(stores, status=NEEDS_INFO)
    await stores.submissions.set_decision(submission.submission_id, status=NEEDS_INFO, reviewer_id=700, reason="more")

    await stores.submissions.replace_answers(submission.submission_id, answers={"work": ["pack"]}, status=PENDING)

    updated = await stores.submissions.get(submission.submission_id)
    assert updated is not None
    assert updated.answers == {"work": ["pack"]}
    assert updated.status == PENDING
    assert updated.decision_reason is None and updated.reviewed_by is None


async def test_decisions_are_recorded(stores) -> None:
    submission = await create_submission(stores)

    await stores.submissions.set_decision(submission.submission_id, status=APPROVED, reviewer_id=700, reason=None)

    updated = await stores.submissions.get(submission.submission_id)
    assert updated is not None
    assert updated.status == APPROVED
    assert updated.reviewed_by == 700
    assert updated.reviewed_at is not None
    assert updated.is_pending is False


async def test_listing_and_counting_can_filter_by_status(stores) -> None:
    await create_submission(stores, discord_user_id=300, status=PENDING)
    await create_submission(stores, discord_user_id=301, status=RECEIVED)
    await create_submission(stores, form_id="feedback", discord_user_id=302, status=RECEIVED)

    everything = await stores.submissions.list_for_form(GUILD_ID, "team_application")
    pending = await stores.submissions.list_for_form(GUILD_ID, "team_application", status=PENDING)

    assert len(everything) == 2
    assert [item.discord_user_id for item in pending] == [300]
    assert await stores.submissions.count_for_form(GUILD_ID, "team_application") == 2
    assert await stores.submissions.count_for_form(GUILD_ID, "team_application", status=RECEIVED) == 1


async def test_listing_is_paginated_newest_first(stores) -> None:
    first = await create_submission(stores, discord_user_id=300)
    second = await create_submission(stores, discord_user_id=301)

    rows = await stores.submissions.list_for_form(GUILD_ID, "team_application", limit=1)

    assert [item.submission_id for item in rows] == [second.submission_id]
    assert second.submission_id > first.submission_id
