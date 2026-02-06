#!/usr/bin/env python3
"""Create an API key for the eKI API."""

import argparse
import asyncio
import hashlib
import os
import secrets
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.db_models import ApiKeyModel


def generate_api_key() -> tuple[str, str]:
    """Generate API key and its hash."""
    api_key = f"eki_{secrets.token_hex(32)}"
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    return api_key, key_hash


async def insert_api_key(
    *,
    database_url: str,
    user_id: str,
    organization_id: str | None,
    name: str,
    description: str,
    key_hash: str,
    expires_at: datetime,
) -> str:
    """Insert API key record via ORM (parameterized, no SQL string interpolation)."""
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            api_key_model = ApiKeyModel(
                id=uuid4(),
                user_id=user_id,
                organization_id=organization_id,
                key_hash=key_hash,
                name=name,
                description=description,
                is_active=True,
                created_at=datetime.utcnow(),
                expires_at=expires_at,
                usage_count=0,
            )
            session.add(api_key_model)
            await session.commit()
            return str(api_key_model.id)
    finally:
        await engine.dispose()


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Create API key for eKI API")
    parser.add_argument(
        "--insert",
        action="store_true",
        help="Insert key metadata directly into the database using DATABASE_URL",
    )
    return parser.parse_args()


def main() -> None:
    """Generate API key and optionally insert metadata into the database."""
    args = parse_args()

    print("=== eKI API Key Generator ===\n")

    # Get user input
    user_id = input("User ID: ").strip() or "user_123"
    organization_id = input("Organization ID (optional): ").strip() or None
    name = input("Key Name: ").strip() or "API Key"
    description = input("Description (optional): ").strip() or ""
    days_valid_input = input("Days valid (default 365): ").strip() or "365"

    try:
        days_valid = int(days_valid_input)
        if days_valid < 1:
            raise ValueError
    except ValueError:
        print("Invalid number of days, using 365")
        days_valid = 365

    # Generate key
    api_key, key_hash = generate_api_key()

    # Calculate expiration
    expires_at = datetime.utcnow() + timedelta(days=days_valid)

    print("\n" + "=" * 60)
    print("API key generated")
    print("=" * 60)
    print(f"\nAPI Key (store securely, shown once):\n  {api_key}\n")
    print(f"Key Hash (stored in DB):\n  {key_hash}\n")
    print(f"User ID: {user_id}")
    if organization_id:
        print(f"Organization ID: {organization_id}")
    print(f"Name: {name}")
    print(f"Description: {description}")
    print(f"Expires: {expires_at.isoformat()}\n")

    if args.insert:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            print("DATABASE_URL is required when using --insert")
            return

        api_key_id = asyncio.run(
            insert_api_key(
                database_url=database_url,
                user_id=user_id,
                organization_id=organization_id,
                name=name,
                description=description,
                key_hash=key_hash,
                expires_at=expires_at,
            )
        )
        print("Record inserted successfully.")
        print(f"API key row id: {api_key_id}\n")
    else:
        print("Dry run only (no DB write).")
        print("Use --insert to persist key metadata via parameterized ORM insert.\n")

    print("IMPORTANT:")
    print("  - Save the API key securely")
    print("  - The plaintext key cannot be recovered from the database")
    print("  - Rotate keys periodically and limit validity windows")


if __name__ == "__main__":
    main()
