# Secrets Directory

This directory contains Docker secrets for production deployment.

## Setup for Production

1. Generate secure passwords:

```bash
# Database password
python -c "import secrets; print(secrets.token_urlsafe(32))" > db_password.txt

# API secret key
python -c "import secrets; print(secrets.token_urlsafe(32))" > api_secret_key.txt

# Full database URL for api/worker secret-file loading
python -c "import pathlib; pw=pathlib.Path('db_password.txt').read_text().strip(); print(f'postgresql+asyncpg://eki_user:{pw}@postgres:5432/eki_db')" > database_url.txt
```

2. Ensure proper permissions:

```bash
chmod 600 *.txt
```

3. These files are automatically ignored by Git (see `.gitignore`)

## Files

- `db_password.txt` - PostgreSQL database password
- `api_secret_key.txt` - API signing key (internal cryptographic secret)
- `database_url.txt` - Full `DATABASE_URL` consumed via `DATABASE_URL_FILE`

**IMPORTANT**: Never commit these files to version control!
