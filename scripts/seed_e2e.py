"""Only against an isolated test database; credentials supplied via environment."""

import os

from sqlalchemy import select

from app.auth import hasher
from app.models import SessionLocal, User

if os.getenv("PROMO_TEST_MODE") != "1":
    raise SystemExit("Set PROMO_TEST_MODE=1 only in a disposable test environment")
with SessionLocal() as s:
    username = os.environ["E2E_USERNAME"]
    user = s.scalar(select(User).where(User.username == username))
    if not user:
        s.add(User(username=username, password_hash=hasher.hash(os.environ["E2E_PASSWORD"]), role="admin"))
        s.commit()
