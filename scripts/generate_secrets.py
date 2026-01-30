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

    # Create secrets directory
    secrets_dir = Path("secrets")
    secrets_dir.mkdir(exist_ok=True)

    # Write secrets to files
    (secrets_dir / "db_password.txt").write_text(db_password)
    (secrets_dir / "api_secret_key.txt").write_text(api_secret_key)

    # Set permissions (Unix only)
    try:
        (secrets_dir / "db_password.txt").chmod(0o600)
        (secrets_dir / "api_secret_key.txt").chmod(0o600)
    except Exception:
        pass  # Windows doesn't support chmod

    print("✅ Secrets generated successfully!\n")
    print("Files created:")
    print("  - secrets/db_password.txt")
    print("  - secrets/api_secret_key.txt\n")

    print("⚠️  IMPORTANT:")
    print("  - Never commit these files to version control")
    print("  - Store them securely (encrypted vault, secrets manager)")
    print("  - Update your .env.local with these values\n")

    print("Database Password:")
    print(f"  {db_password}\n")

    print("API Secret Key:")
    print(f"  {api_secret_key}\n")

    print("Next steps:")
    print("  1. Copy .env.example to .env.local")
    print("  2. Update .env.local with the secrets above")
    print(
        "  3. Deploy using: docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d"
    )


if __name__ == "__main__":
    main()
