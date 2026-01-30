#!/usr/bin/env python3
"""Create an API key for the eKI API."""

import hashlib
import secrets
import sys
from datetime import datetime, timedelta


def generate_api_key() -> tuple[str, str]:
    """Generate API key and its hash."""
    api_key = f"eki_{secrets.token_hex(32)}"
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    return api_key, key_hash


def main() -> None:
    """Generate API key and print SQL to insert it."""
    print("=== eKI API Key Generator ===\n")
    
    # Get user input
    user_id = input("User ID: ").strip() or "user_123"
    organization_id = input("Organization ID (optional): ").strip() or None
    name = input("Key Name: ").strip() or "API Key"
    description = input("Description (optional): ").strip() or ""
    days_valid = input("Days valid (default 365): ").strip() or "365"
    
    try:
        days_valid = int(days_valid)
    except ValueError:
        print("Invalid number of days, using 365")
        days_valid = 365
    
    # Generate key
    api_key, key_hash = generate_api_key()
    
    # Calculate expiration
    expires_at = datetime.utcnow() + timedelta(days=days_valid)
    
    # Print results
    print("\n" + "=" * 60)
    print("✅ API Key Generated Successfully!")
    print("=" * 60)
    
    print(f"\nAPI Key (save this, it won't be shown again):")
    print(f"  {api_key}\n")
    
    print(f"Key Hash (stored in database):")
    print(f"  {key_hash}\n")
    
    print(f"User ID: {user_id}")
    if organization_id:
        print(f"Organization ID: {organization_id}")
    print(f"Name: {name}")
    print(f"Description: {description}")
    print(f"Expires: {expires_at.isoformat()}\n")
    
    # Generate SQL
    org_sql = f"'{organization_id}'" if organization_id else "NULL"
    desc_sql = f"'{description}'" if description else "''"
    
    sql = f"""INSERT INTO api_keys (
  id,
  user_id,
  organization_id,
  key_hash,
  name,
  description,
  is_active,
  expires_at,
  created_at
) VALUES (
  gen_random_uuid(),
  '{user_id}',
  {org_sql},
  '{key_hash}',
  '{name}',
  {desc_sql},
  true,
  '{expires_at.isoformat()}',
  NOW()
);"""
    
    print("SQL to insert into database:")
    print("=" * 60)
    print(sql)
    print("=" * 60)
    
    print("\nTo insert this key, run:")
    print('docker compose exec -e PGPASSWORD=<db_password> postgres \\')
    print('  psql -U eki_user -d eki_db -c "' + sql.replace('"', '\\"') + '"')
    
    print("\n⚠️  IMPORTANT:")
    print("  - Save the API key securely")
    print("  - It cannot be retrieved after this")
    print("  - The key hash is stored in the database")


if __name__ == "__main__":
    main()
