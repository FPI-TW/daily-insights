import argparse
import asyncio
import sys

from sqlalchemy import select

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.core.security import (
    generate_temporary_password,
    hash_password,
    normalize_email,
)
from daily_insights_api.modules.audit.service import record_audit_event
from daily_insights_api.modules.identity.models import User


async def bootstrap_admin(email: str, display_name: str) -> str:
    settings = get_settings()
    assert settings.password_pepper is not None
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as database:
            normalized_email = normalize_email(email)
            existing = await database.scalar(select(User).where(User.email == normalized_email))
            if existing is not None:
                raise ValueError("a user with this email already exists")
            temporary_password = generate_temporary_password()
            admin = User(
                email=normalized_email,
                display_name=display_name.strip(),
                password_hash=hash_password(
                    temporary_password,
                    settings.password_pepper.get_secret_value(),
                ),
                must_change_password=True,
                system_role=SystemRole.ADMIN,
                status=UserStatus.ACTIVE,
            )
            database.add(admin)
            await database.flush()
            record_audit_event(
                database,
                actor_user_id=admin.id,
                action="identity.admin_bootstrapped",
                target_type="user",
                target_id=str(admin.id),
                after={"email": admin.email, "system_role": admin.system_role.value},
                request_id="bootstrap-admin-cli",
            )
            await database.commit()
            return temporary_password
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the first Daily Insights admin")
    parser.add_argument("--email", required=True)
    parser.add_argument("--display-name", required=True)
    arguments = parser.parse_args()
    try:
        temporary_password = asyncio.run(bootstrap_admin(arguments.email, arguments.display_name))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
    print("Admin created. This temporary password is shown once:")
    print(temporary_password)


if __name__ == "__main__":
    main()
