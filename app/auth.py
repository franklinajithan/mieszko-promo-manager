import hashlib
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Depends, HTTPException, Request

from app.models import LoginSession, User, db, now

hasher = PasswordHasher()
DUMMY = hasher.hash(secrets.token_urlsafe(24))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def verify(encoded, password):
    try:
        return hasher.verify(encoded or DUMMY, password)
    except VerificationError:
        return False


def current_user(request: Request, s=Depends(db)):
    token = request.cookies.get("promo_session", "")
    session = s.get(LoginSession, digest(token)) if token else None
    if not session or session.expires_at.replace(tzinfo=now().tzinfo) <= now():
        raise HTTPException(401, "Please sign in")
    user = s.get(User, session.user_id)
    if not user or not user.active:
        raise HTTPException(401, "Account unavailable")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session.csrf):
            raise HTTPException(403, "Invalid security token; reload the page")
    request.state.login_session = session
    return user


def admin(user=Depends(current_user)):
    if user.role != "admin":
        raise HTTPException(403, "Administrator access required")
    return user


def new_session(s, user, hours):
    token = secrets.token_urlsafe(48)
    session = LoginSession(
        token_hash=digest(token),
        user_id=user.id,
        csrf=secrets.token_urlsafe(32),
        expires_at=now() + timedelta(hours=hours),
    )
    s.add(session)
    return token, session
