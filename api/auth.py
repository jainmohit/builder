import hashlib, hmac, os, re, secrets, time

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from dotenv import load_dotenv
from fastapi import HTTPException, Request, Response

from . import db

load_dotenv()
ph = PasswordHasher()
DUMMY = ph.hash("dummy-password-for-timing")
COOKIE = "session"
SESSION_DAYS = 14
FAILS: dict[str, list[float]] = {}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()    # only the hash is stored


def too_many_failures(key: str) -> bool:
    now = time.time()
    FAILS[key] = [t for t in FAILS.get(key, []) if now - t < 900]
    return len(FAILS[key]) >= 5


def create_session(response: Response, user_id: int):
    db.x("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
    token = secrets.token_urlsafe(32)
    db.x("INSERT INTO sessions(token_hash, user_id, expires_at) VALUES(?,?,?)",
         (_hash(token), user_id, time.time() + SESSION_DAYS * 86400))
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=os.getenv("COOKIE_SECURE") == "1", path="/")


def current_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE)
    if token:
        rows = db.q("SELECT u.id, u.email, u.is_admin FROM sessions s JOIN users u ON u.id=s.user_id "
                    "WHERE s.token_hash=? AND s.expires_at>?", (_hash(token), time.time()))
        if rows:
            return rows[0]
    raise HTTPException(401, "not logged in")


def register(email: str, password: str, code: str) -> int:
    expected = os.getenv("SIGNUP_CODE", "")
    if not expected:
        raise HTTPException(403, "Signups are closed (SIGNUP_CODE is not set on the server).")
    if not hmac.compare_digest(code.encode(), expected.encode()):
        raise HTTPException(403, "Invalid invite code.")
    email = email.strip().lower()
    if len(email) > 200 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "Enter a valid email address.")
    if not 10 <= len(password) <= 200:
        raise HTTPException(400, "Password must be 10 to 200 characters.")
    if db.q("SELECT 1 FROM users WHERE email=?", (email,)):
        raise HTTPException(409, "An account with that email already exists.")
    first = not db.q("SELECT 1 FROM users LIMIT 1")        # the first account is the admin
    uid = db.x("INSERT INTO users(email, pw_hash, is_admin, created_at) VALUES(?,?,?,?)",
               (email, ph.hash(password), 1 if first else 0, time.time()))
    if first:
        db.x("UPDATE projects SET user_id=? WHERE user_id IS NULL", (uid,))   # adopt your existing projects
    return uid


def login(email: str, password: str, ip: str) -> int:
    email = email.strip().lower()
    key = f"{ip}|{email}"
    if too_many_failures(key):
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    rows = db.q("SELECT id, pw_hash FROM users WHERE email=?", (email,))
    row = rows[0] if rows else None
    try:
        ph.verify(row["pw_hash"] if row else DUMMY, password)   # same work either way, so timing doesn't reveal accounts
        if row:
            return row["id"]
    except VerifyMismatchError:
        pass
    FAILS.setdefault(key, []).append(time.time())
    raise HTTPException(401, "Wrong email or password.")


def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        db.x("DELETE FROM sessions WHERE token_hash=?", (_hash(token),))
    response.delete_cookie(COOKIE, path="/")