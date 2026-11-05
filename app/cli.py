import argparse
import getpass

from sqlalchemy import select

from app.auth import hasher
from app.models import SessionLocal, User


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["create-admin"])
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    password = getpass.getpass("New password (12+ characters): ")
    if len(password) < 12 or password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords must match and contain at least 12 characters")
    with SessionLocal() as s:
        if s.scalar(select(User).where(User.username == args.username.lower())):
            raise SystemExit("Username already exists")
        s.add(User(username=args.username.lower(), password_hash=hasher.hash(password), role="admin"))
        s.commit()
    print("Administrator created")


if __name__ == "__main__":
    main()
