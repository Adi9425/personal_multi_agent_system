from db.models import UsageRecord
from db.session import async_session


async def record_usage(*, user_id: int, action: str, model: str, input_tokens: int, output_tokens: int) -> None:
    """The only writer of usage_records (same single-choke-point pattern as D5). This table
    is both the trial-limit source of truth (services/users.check_access queries it
    directly) and the raw data for real per-user/per-action cost analysis."""
    async with async_session() as session:
        session.add(
            UsageRecord(
                user_id=user_id,
                action=action,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )
        await session.commit()
