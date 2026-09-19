import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from core.schemas import UserStatus
from db.models import UsageRecord, User, UserProfile
from db.session import async_session, tenant_session

AccessDecision = Literal["allowed", "trial_expired", "suspended"]

# UserProfile.settings is a free-form JSONB extension point (see its own docstring) — this
# flag distinguishes "never contacted" (prompt for a name) from "prompted, their next message
# IS the answer" without a dedicated column or Redis state. Cleared once display_name is set.
_ONBOARDING_ASKED_KEY = "onboarding_asked"


def is_admin(telegram_user_id: int) -> bool:
    return telegram_user_id in settings.admin_telegram_user_id_set


def _by_telegram_id(telegram_user_id: int) -> Select[tuple[User]]:
    # profile comes along via the joined relationship. populate_existing refreshes objects
    # expired by a commit, so the returned User is fully loaded before the session closes.
    return (
        select(User)
        .where(User.telegram_user_id == telegram_user_id)
        .execution_options(populate_existing=True)
    )


async def get_or_create_user(
    telegram_user_id: int,
    telegram_username: str | None,
    first_name: str | None = None,
    last_name: str | None = None,
    language_code: str | None = None,
) -> User:
    """The only writer of users and user_profiles (same single-choke-point pattern as D5).

    This is the one place the Telegram ID is used as a lookup key. It returns a User with
    .profile loaded; everything downstream (entries, usage, tenant_session) takes user.id."""
    should_be_admin = is_admin(telegram_user_id)

    async with async_session() as session:
        user = await session.scalar(_by_telegram_id(telegram_user_id))

        if user is None:
            await _register(
                session,
                telegram_user_id=telegram_user_id,
                admin=should_be_admin,
                telegram_username=telegram_username,
                first_name=first_name,
                last_name=last_name,
                language_code=language_code,
            )
            await session.commit()
            return (await session.scalars(_by_telegram_id(telegram_user_id))).one()

        # Telegram fields can change and admin status comes from config — both self-heal on
        # every call rather than only at creation time.
        changed = _sync_telegram_fields(
            user.profile,
            telegram_username=telegram_username,
            first_name=first_name,
            last_name=last_name,
        )
        if user.is_admin != should_be_admin:
            user.is_admin = should_be_admin
            changed = True
        if user.bot_blocked_at is not None:  # they messaged us, so they've unblocked the bot
            user.bot_blocked_at = None
            changed = True

        if changed:
            await session.commit()
            user = (await session.scalars(_by_telegram_id(telegram_user_id))).one()
        return user


async def _register(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    admin: bool,
    telegram_username: str | None,
    first_name: str | None,
    last_name: str | None,
    language_code: str | None,
) -> None:
    # ON CONFLICT: a brand-new user's first two updates can be processed concurrently by
    # different arq workers. The loser gets no id back and skips the profile insert; the
    # winner creates both rows in one transaction, so the re-select sees a complete user.
    user_id = await session.scalar(
        insert(User)
        .values(
            telegram_user_id=telegram_user_id,
            is_admin=admin,
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=settings.trial_days),
        )
        .on_conflict_do_nothing(index_elements=[User.telegram_user_id])
        .returning(User.id)
    )
    if user_id is None:
        return

    session.add(
        UserProfile(
            user_id=user_id,
            telegram_username=telegram_username,
            first_name=first_name,
            last_name=last_name,
            language=language_code or "en",
        )
    )


def _sync_telegram_fields(profile: UserProfile, **fields: str | None) -> bool:
    """None means "the caller didn't supply it", not "Telegram cleared it", so it never
    overwrites. Same behavior the original username sync had."""
    changed = False
    for name, value in fields.items():
        if value is not None and getattr(profile, name) != value:
            setattr(profile, name, value)
            changed = True
    return changed


def needs_onboarding_prompt(user: User) -> bool:
    """True exactly once, on a brand-new user's first message ever."""
    return user.profile.display_name is None and not user.profile.settings.get(_ONBOARDING_ASKED_KEY)


def is_awaiting_name(user: User) -> bool:
    """True for the one message right after the onboarding prompt — treat it as their
    answer, not as something to capture/query/update."""
    return user.profile.display_name is None and bool(user.profile.settings.get(_ONBOARDING_ASKED_KEY))


async def mark_onboarding_prompted(user_id: uuid.UUID) -> None:
    async with async_session() as session:
        profile = await session.get(UserProfile, user_id)
        profile.settings = {**profile.settings, _ONBOARDING_ASKED_KEY: True}
        await session.commit()


async def set_display_name(user_id: uuid.UUID, display_name: str) -> None:
    async with async_session() as session:
        profile = await session.get(UserProfile, user_id)
        profile.display_name = display_name
        # No longer needed once display_name is set (needs_onboarding_prompt/is_awaiting_name
        # both already key off display_name being non-null first) — dropped anyway so the
        # flag doesn't linger as dead state.
        profile.settings = {k: v for k, v in profile.settings.items() if k != _ONBOARDING_ASKED_KEY}
        await session.commit()


async def mark_bot_blocked(telegram_user_id: int) -> None:
    """Call from the sender on TelegramForbiddenError so jobs stop retrying a dead chat.
    Takes the Telegram ID because that's what the send context has (chat_id == user id in
    private chats)."""
    async with async_session() as session:
        await session.execute(
            update(User)
            .where(User.telegram_user_id == telegram_user_id, User.bot_blocked_at.is_(None))
            .values(bot_blocked_at=func.now())
        )
        await session.commit()


async def suspend_user(user_id: uuid.UUID, reason: str) -> None:
    async with async_session() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=UserStatus.suspended, suspended_at=func.now(), suspension_reason=reason)
        )
        await session.commit()


async def reinstate_user(user_id: uuid.UUID, new_status: UserStatus) -> None:
    if new_status == UserStatus.suspended:
        raise ValueError("use suspend_user to suspend a user")
    async with async_session() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=new_status, suspended_at=None, suspension_reason=None)
        )
        await session.commit()


async def check_access(user: User) -> AccessDecision:
    """Trial expiry is checked live against usage_records (the ledger services/llm.py and
    services/embeddings.py write to) — no separate counter that could drift out of sync."""
    if user.is_admin:
        return "allowed"
    if user.status == UserStatus.suspended:
        return "suspended"
    if user.status == UserStatus.active:
        return "allowed"

    now = datetime.now(timezone.utc)
    if user.trial_ends_at is not None and now > user.trial_ends_at:
        return "trial_expired"

    async with tenant_session(user.id) as session:
        result = await session.execute(
            select(func.count()).select_from(UsageRecord).where(UsageRecord.user_id == user.id)
        )
        request_count = result.scalar_one()

    if request_count >= settings.trial_request_limit:
        return "trial_expired"

    return "allowed"
