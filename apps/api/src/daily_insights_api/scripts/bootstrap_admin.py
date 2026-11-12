import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.core.security import (
    hash_password,
    normalize_email,
    validate_account_password,
)
from daily_insights_api.modules.audit.service import record_audit_event
from daily_insights_api.modules.identity.models import User


async def bootstrap_admin(email: str, display_name: str, password: str) -> None:
    validate_account_password(password)
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
            admin = User(
                email=normalized_email,
                display_name=display_name.strip(),
                password_hash=hash_password(
                    password,
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
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the first Daily Insights admin")
    parser.add_argument("--email", required=True)
    parser.add_argument("--display-name", required=True)
    arguments = parser.parse_args()
    try:
        password = getpass.getpass("Initial password: ")
        confirmation = getpass.getpass("Confirm initial password: ")
        if password != confirmation:
            raise ValueError("password confirmation does not match")
        asyncio.run(bootstrap_admin(arguments.email, arguments.display_name, password))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
    except (EOFError, KeyboardInterrupt) as error:
        print("password input is required", file=sys.stderr)
        raise SystemExit(1) from error
    print("Admin created. The initial password must be changed after first login.")


if __name__ == "__main__":
    main()
