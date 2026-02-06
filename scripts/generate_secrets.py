#!/usr/bin/env python3
"""Generate secure secrets for production deployment."""

import secrets
from pathlib import Path


def generate_secret(length: int = 32) -> str:
    """Generate a secure random secret."""
    return secrets.token_urlsafe(length)


def main() -> None:
    """Generate all required secrets for production."""
    print("=== eKI API Secret Generator ===\n")

    # Generate secrets
    db_password = generate_secret(32)
    api_secret_key = generate_secret(32)
    database_url = f"postgresql+asyncpg://eki_user:{db_password}@postgres:5432/eki_db"

    # Create secrets directory
    secrets_dir = Path("secrets")
    secrets_dir.mkdir(exist_ok=True)

    # Write secrets to files
    (secrets_dir / "db_password.txt").write_text(db_password)
    (secrets_dir / "api_secret_key.txt").write_text(api_secret_key)
    (secrets_dir / "database_url.txt").write_text(database_url)

    # Set permissions (Unix only)
    try:
        (secrets_dir / "db_password.txt").chmod(0o600)
        (secrets_dir / "api_secret_key.txt").chmod(0o600)
        (secrets_dir / "database_url.txt").chmod(0o600)
    except Exception:
        pass  # Windows doesn't support chmod

    print("✅ Secrets generated successfully!\n")
    print("Files created:")
    print("  - secrets/db_password.txt")
    print("  - secrets/api_secret_key.txt\n")
    print("  - secrets/database_url.txt\n")

    print("⚠️  IMPORTANT:")
    print("  - Never commit these files to version control")
    print("  - Store them securely (encrypted vault, secrets manager)")
    print("  - Avoid printing secrets in CI/CD logs")
    print("  - Use *_FILE variables in production (Docker secrets)\n")

    print("Next steps:")
    print("  1. Copy .env.example to .env.local (development only)")
    print("  2. For production, mount secrets and set DATABASE_URL_FILE/API_SECRET_KEY_FILE")
    print(
        "  3. Deploy using: docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d"
    )


if __name__ == "__main__":
    main()
