#!/usr/bin/env python3
"""Create local settings with random credentials; never overwrite existing settings."""
import os
from pathlib import Path
import secrets


def main():
    root = Path(__file__).resolve().parent.parent
    target = root / '.env'
    if target.exists():
        raise SystemExit('.env already exists; edit it directly. Nothing was changed.')
    text = (root / '.env.example').read_text()
    text = text.replace('SYNC_PASSWORD=\n', 'SYNC_PASSWORD=' + secrets.token_urlsafe(24) + '\n')
    text = text.replace('SYNC_API_KEY=\n', 'SYNC_API_KEY=' + secrets.token_urlsafe(32) + '\n')
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit('.env already exists; nothing was changed.')
    with os.fdopen(fd, 'w') as output:
        output.write(text)
    print('Created .env with independent random credentials. Credentials were not printed.')
    print('For trusted LAN access, edit BOOKHUB_BIND before running docker compose up -d --build.')


if __name__ == '__main__':
    main()
