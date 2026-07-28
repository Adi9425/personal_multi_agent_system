from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import func, select

from app.config import settings
from core.schemas import UserStatus
from db.models import UsageRecord, User
from db.session import async_session

AccessDecision = Literal["allowed", "trial_expired", "suspended"]


def is_admin(telegram_user_id: int) -> bool:
    return telegram_user_id in settings.admin_telegram_user_id_set


async def get_or_create_user(telegram_user_id: int, telegram_username: str | None) -> User:
    """The only writer of the users table (same single-choke-point pattern as D5)."""
    async with async_session() as session:
        user = await session.get(User, telegram_user_id)
        should_be_admin = is_admin(telegram_user_id)

        if user is not None:
            # Username can change; admin status can be granted/revoked via config — keep
            # both self-healing on every check rather than only at creation time.
            changed = False
            if telegram_username and user.telegram_username != telegram_username:
                user.telegram_username = telegram_username
                changed = True
            if user.is_admin != should_be_admin:
                user.is_admin = should_be_admin
                changed = True
            if changed:
                await session.commit()
                await session.refresh(user)
            return user

        user = User(
            telegram_user_id=telegram_user_id,
            telegram_username=telegram_username,
            is_admin=should_be_admin,
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=settings.trial_days),
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


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

    async with async_session() as session:
        result = await session.execute(
            select(func.count()).select_from(UsageRecord).where(UsageRecord.user_id == user.telegram_user_id)
        )
        request_count = result.scalar_one()

    if request_count >= settings.trial_request_limit:
        return "trial_expired"

    return "allowed"
