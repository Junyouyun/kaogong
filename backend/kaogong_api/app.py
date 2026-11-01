from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parents[2]
QUESTION_DB = Path(
    os.environ.get(
        "KAOGONG_DB",
        str(ROOT / "data/processed/question_bank.db"),
    )
)
USER_DB = Path(
    os.environ.get(
        "KAOGONG_USER_DB",
        str(ROOT / "data/processed/user_state.db"),
    )
)
WEB_DIR = ROOT / "web"
IMAGE_ROOTS = {
    "gongkao-naoku": (ROOT / "data/external/gongkao-naoku").resolve(),
    "errrc-kaogong-shuati": (
        ROOT / "data/external/errrc-kaogong-shuati"
    ).resolve(),
}
IMAGE_SUFFIXES = {".webp", ".png", ".jpg", ".jpeg", ".gif", ".bmp"}
IMAGE_MEDIA_TYPES = {
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

SESSION_COOKIE = "kaogong_session"
SESSION_DAYS = 30
PASSWORD_ITERATIONS = 260_000
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 8
LOGIN_FAILURES: dict[tuple[str, str], list[datetime]] = {}


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class Registration(Credentials):
    invite_code: str = ""


class AttemptRequest(BaseModel):
    selected_answer: list[str] = Field(default_factory=list)
    free_text: str = ""
    elapsed_ms: int | None = Field(default=None, ge=0)


class PracticeSessionRequest(BaseModel):
    paper_id: int
    mode: Literal["practice", "exam"] = "practice"
    duration_seconds: int | None = Field(default=None, ge=60, le=86400)


class InviteCreateRequest(BaseModel):
    expires_days: int = Field(default=7, ge=1, le=30)


app = FastAPI(title="Kaogong API", version="0.2.0")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def question_connection() -> sqlite3.Connection:
    if not QUESTION_DB.is_file():
        raise HTTPException(
            status_code=503,
            detail="题库尚未构建，请先运行 scripts/build_question_bank.py",
        )
    connection = sqlite3.connect(f"file:{QUESTION_DB.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def user_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(USER_DB)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def ensure_user_db() -> None:
    USER_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = user_connection()
    connection.executescript(
        """
        PRAGMA journal_mode = WAL;

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
            ON auth_sessions(user_id);

        CREATE TABLE IF NOT EXISTS invite_codes (
            id INTEGER PRIMARY KEY,
            code_hash TEXT NOT NULL UNIQUE,
            code_prefix TEXT NOT NULL,
            created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_by INTEGER REFERENCES users(id),
            used_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_invite_codes_created
            ON invite_codes(created_at DESC);

        CREATE TABLE IF NOT EXISTS practice_sessions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            paper_id INTEGER NOT NULL,
            mode TEXT NOT NULL CHECK (mode IN ('practice', 'exam')),
            question_ids_json TEXT NOT NULL,
            duration_seconds INTEGER,
            started_at TEXT NOT NULL,
            submitted_at TEXT,
            status TEXT NOT NULL DEFAULT 'active'
        );

        CREATE INDEX IF NOT EXISTS idx_practice_sessions_user
            ON practice_sessions(user_id, id DESC);

        CREATE TABLE IF NOT EXISTS wrong_book (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            question_id INTEGER NOT NULL,
            wrong_count INTEGER NOT NULL DEFAULT 1,
            correct_after_wrong INTEGER NOT NULL DEFAULT 0,
            resolved INTEGER NOT NULL DEFAULT 0,
            last_wrong_at TEXT NOT NULL,
            last_correct_at TEXT,
            PRIMARY KEY (user_id, question_id)
        );

        CREATE INDEX IF NOT EXISTS idx_wrong_book_user
            ON wrong_book(user_id, resolved, last_wrong_at DESC);
        """
    )
    user_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(users)").fetchall()
    }
    if "is_admin" not in user_columns:
        connection.execute(
            "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
        )
    admin_count = connection.execute(
        "SELECT COUNT(*) FROM users WHERE is_admin = 1"
    ).fetchone()[0]
    if admin_count == 0:
        first_user = connection.execute(
            "SELECT id FROM users ORDER BY id LIMIT 1"
        ).fetchone()
        if first_user:
            connection.execute(
                "UPDATE users SET is_admin = 1 WHERE id = ?",
                (first_user["id"],),
            )
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(attempts)").fetchall()
    }
    if not columns:
        connection.execute(
            """
            CREATE TABLE attempts (
                id INTEGER PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                practice_session_id INTEGER REFERENCES practice_sessions(id),
                question_id INTEGER NOT NULL,
                selected_answer TEXT NOT NULL DEFAULT '[]',
                free_text TEXT NOT NULL DEFAULT '',
                is_correct INTEGER,
                elapsed_ms INTEGER,
                answered_at TEXT NOT NULL
            )
            """
        )
    else:
        if "user_id" not in columns:
            connection.execute(
                "ALTER TABLE attempts ADD COLUMN user_id INTEGER REFERENCES users(id)"
            )
        if "practice_session_id" not in columns:
            connection.execute(
                """
                ALTER TABLE attempts
                ADD COLUMN practice_session_id INTEGER REFERENCES practice_sessions(id)
                """
            )
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_attempts_user
            ON attempts(user_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_attempts_session
            ON attempts(practice_session_id, question_id, id DESC);
        """
    )
    connection.commit()
    connection.close()


@app.on_event("startup")
def startup() -> None:
    ensure_user_db()


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def password_matches(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


def session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_auth_session(user_id: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(
        timespec="seconds"
    )
    connection = user_connection()
    try:
        connection.execute(
            """
            INSERT INTO auth_sessions(token_hash, user_id, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_hash(token), user_id, expires_at, utc_now()),
        )
        connection.commit()
    finally:
        connection.close()
    return token, expires_at


def resolve_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    connection = user_connection()
    try:
        row = connection.execute(
            """
            SELECT u.id, u.username, u.is_admin, s.expires_at
            FROM auth_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (session_hash(token),),
        ).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?",
                (session_hash(token),),
            )
            connection.commit()
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "is_admin": bool(row["is_admin"]),
        }
    finally:
        connection.close()


def set_session_cookie(response: JSONResponse, request: Request, token: str) -> None:
    forwarded = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=forwarded == "https",
        path="/",
    )


def login_key(payload: Credentials, request: Request) -> tuple[str, str]:
    client = request.client.host if request.client else "unknown"
    return payload.username.strip().lower(), client


def recent_login_failures(key: tuple[str, str]) -> list[datetime]:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=LOGIN_WINDOW_SECONDS)
    failures = [value for value in LOGIN_FAILURES.get(key, []) if value > cutoff]
    if failures:
        LOGIN_FAILURES[key] = failures
    else:
        LOGIN_FAILURES.pop(key, None)
    return failures


@app.middleware("http")
async def require_auth(request: Request, call_next):
    path = request.url.path
    public_api = (
        path == "/api/health"
        or path == "/api/auth/status"
        or path.startswith("/api/auth/login")
        or path.startswith("/api/auth/register")
    )
    if path.startswith("/api/") and not public_api:
        user = resolve_user(request)
        if user is None:
            return JSONResponse(status_code=401, content={"detail": "请先登录"})
        request.state.user = user
    return await call_next(request)


def fetch_options(
    connection: sqlite3.Connection, question_id: int
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT option_key, label, html, is_answer, sort_order
        FROM question_options
        WHERE question_id = ?
        ORDER BY sort_order
        """,
        (question_id,),
    )
    return [
        {
            "key": row["option_key"],
            "label": row["label"],
            "html": row["html"],
            "is_answer": bool(row["is_answer"]),
            "sort_order": row["sort_order"],
        }
        for row in rows
    ]


def fetch_assets(
    connection: sqlite3.Connection, question_id: int
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT a.source_id, a.source_ref, a.local_path, a.exists_locally
        FROM assets a
        JOIN question_assets qa ON qa.asset_id = a.id
        WHERE qa.question_id = ?
        ORDER BY a.id
        """,
        (question_id,),
    )
    result = []
    for row in rows:
        relative = row["local_path"].split("/", 3)
        api_path = (
            f"/api/images/{row['source_id']}/{relative[3]}"
            if len(relative) == 4
            else None
        )
        result.append(
            {
                "source_ref": row["source_ref"],
                "local_path": row["local_path"],
                "exists_locally": bool(row["exists_locally"]),
                "url": api_path,
            }
        )
    return result


def fetch_materials(
    connection: sqlite3.Connection, paper_id: int | None
) -> list[dict]:
    if paper_id is None:
        return []
    rows = connection.execute(
        """
        SELECT material_index, title, text, metadata_json
        FROM materials
        WHERE paper_id = ?
        ORDER BY material_index
        """,
        (paper_id,),
    )
    return [
        {
            "index": row["material_index"],
            "title": row["title"],
            "text": row["text"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
        }
        for row in rows
    ]


def question_payload(
    connection: sqlite3.Connection,
    question_id: int,
    *,
    reveal: bool,
) -> dict:
    row = connection.execute(
        """
        SELECT q.*, p.title AS paper_title, p.region, p.year,
               p.duration_seconds, p.declared_question_count
        FROM questions q
        LEFT JOIN papers p ON p.id = q.primary_paper_id
        WHERE q.id = ?
        """,
        (question_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="题目不存在")
    options = fetch_options(connection, question_id)
    result = {
        "id": row["id"],
        "source_question_key": row["source_question_key"],
        "subject": row["subject"],
        "module": row["module"],
        "category": row["category"],
        "knowledge_point": row["knowledge_point"],
        "stem_html": row["stem_html"],
        "stem_text": row["stem_text"],
        "passage_html": row["passage_html"],
        "question_type": row["question_type"],
        "difficulty": row["difficulty"],
        "quality_status": row["quality_status"],
        "paper": {
            "id": row["primary_paper_id"],
            "title": row["paper_title"],
            "region": row["region"],
            "year": row["year"],
            "duration_seconds": row["duration_seconds"],
            "question_count": row["declared_question_count"],
        },
        "options": [
            {key: value for key, value in option.items() if key != "is_answer"}
            for option in options
        ],
        "assets": fetch_assets(connection, question_id),
        "materials": fetch_materials(connection, row["primary_paper_id"]),
    }
    if reveal:
        result.update(
            {
                "answer_text": row["answer_text"],
                "correct_option_keys": [
                    option["key"] for option in options if option["is_answer"]
                ],
                "analysis_text": row["analysis_text"],
                "analysis_html": row["analysis_html"],
            }
        )
    return result


def evaluate_answer(
    connection: sqlite3.Connection,
    question: sqlite3.Row,
    selected: list[str],
    free_text: str,
) -> tuple[bool | None, list[str]]:
    correct_keys = [
        row["option_key"]
        for row in connection.execute(
            """
            SELECT option_key
            FROM question_options
            WHERE question_id = ? AND is_answer = 1
            ORDER BY option_key
            """,
            (question["id"],),
        )
    ]
    if question["question_type"] in {"single", "multi", "judge"}:
        is_correct = bool(correct_keys) and sorted(selected) == sorted(correct_keys)
    elif question["question_type"] == "blank":
        normalize = lambda value: re.sub(r"\s+", "", value or "")
        is_correct = normalize(free_text) == normalize(question["answer_text"])
    else:
        is_correct = None
    return is_correct, correct_keys


def record_wrong_book(
    user_id: int,
    question_id: int,
    is_correct: bool | None,
) -> None:
    if is_correct is None:
        return
    connection = user_connection()
    try:
        if is_correct:
            connection.execute(
                """
                UPDATE wrong_book
                SET correct_after_wrong = correct_after_wrong + 1,
                    last_correct_at = ?
                WHERE user_id = ? AND question_id = ?
                """,
                (utc_now(), user_id, question_id),
            )
        else:
            connection.execute(
                """
                INSERT INTO wrong_book
                    (user_id, question_id, wrong_count, correct_after_wrong,
                     resolved, last_wrong_at)
                VALUES (?, ?, 1, 0, 0, ?)
                ON CONFLICT(user_id, question_id) DO UPDATE SET
                    wrong_count = wrong_count + 1,
                    resolved = 0,
                    last_wrong_at = excluded.last_wrong_at
                """,
                (user_id, question_id, utc_now()),
            )
        connection.commit()
    finally:
        connection.close()


def save_attempt(
    *,
    user_id: int,
    question_id: int,
    selected: list[str],
    free_text: str,
    is_correct: bool | None,
    elapsed_ms: int | None,
    practice_session_id: int | None = None,
) -> int:
    connection = user_connection()
    try:
        cursor = connection.execute(
            """
            INSERT INTO attempts
                (user_id, practice_session_id, question_id, selected_answer,
                 free_text, is_correct, elapsed_ms, answered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                practice_session_id,
                question_id,
                json.dumps(selected, ensure_ascii=False),
                free_text,
                None if is_correct is None else int(is_correct),
                elapsed_ms,
                utc_now(),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


@app.get("/api/health")
def health() -> dict:
    connection = question_connection()
    try:
        counts = connection.execute(
            "SELECT subject, COUNT(*) AS count FROM questions GROUP BY subject"
        ).fetchall()
        return {
            "status": "ok",
            "questions": {row["subject"]: row["count"] for row in counts},
        }
    finally:
        connection.close()


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict:
    connection = user_connection()
    try:
        user_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active_invites = connection.execute(
            """
            SELECT COUNT(*) FROM invite_codes
            WHERE used_at IS NULL AND expires_at > ?
            """,
            (utc_now(),),
        ).fetchone()[0]
    finally:
        connection.close()
    current = resolve_user(request)
    setup_enabled = bool(os.environ.get("KAOGONG_SETUP_TOKEN"))
    return {
        "initialized": user_count > 0,
        "registration_open": user_count == 0 or active_invites > 0,
        "invite_required": user_count > 0,
        "setup_required": user_count == 0 and setup_enabled,
        "active_invites": active_invites,
        "authenticated": current is not None,
        "user": current,
    }


@app.post("/api/auth/register")
def register(payload: Registration, request: Request) -> JSONResponse:
    connection = user_connection()
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        user_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        setup_token = os.environ.get("KAOGONG_SETUP_TOKEN", "")
        invite_row = None
        if user_count == 0:
            if setup_token and not hmac.compare_digest(
                payload.invite_code, setup_token
            ):
                raise HTTPException(status_code=403, detail="需要有效的首次部署码")
            is_admin = 1
        else:
            invite_row = connection.execute(
                """
                SELECT id FROM invite_codes
                WHERE code_hash = ? AND used_at IS NULL AND expires_at > ?
                """,
                (session_hash(payload.invite_code.strip()), utc_now()),
            ).fetchone()
            if invite_row is None:
                raise HTTPException(status_code=403, detail="注册需要有效邀请码")
            is_admin = 0
        try:
            cursor = connection.execute(
                """
                INSERT INTO users(username, password_hash, is_admin, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    payload.username.strip(),
                    password_hash(payload.password),
                    is_admin,
                    utc_now(),
                ),
            )
            user_id = int(cursor.lastrowid)
            if invite_row is not None:
                connection.execute(
                    """
                    UPDATE invite_codes
                    SET used_by = ?, used_at = ?
                    WHERE id = ?
                    """,
                    (user_id, utc_now(), invite_row["id"]),
                )
            connection.commit()
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise HTTPException(status_code=409, detail="用户名已存在") from error
    except HTTPException:
        connection.rollback()
        raise
    finally:
        connection.close()
    token, _ = create_auth_session(user_id)
    response = JSONResponse(
        {
            "user": {
                "id": user_id,
                "username": payload.username.strip(),
                "is_admin": bool(is_admin),
            }
        }
    )
    set_session_cookie(response, request, token)
    return response


@app.post("/api/auth/login")
def login(payload: Credentials, request: Request) -> JSONResponse:
    key = login_key(payload, request)
    if len(recent_login_failures(key)) >= LOGIN_MAX_FAILURES:
        raise HTTPException(status_code=429, detail="登录失败次数过多，请稍后再试")
    connection = user_connection()
    try:
        row = connection.execute(
            "SELECT id, username, password_hash, is_admin FROM users WHERE username = ?",
            (payload.username.strip(),),
        ).fetchone()
    finally:
        connection.close()
    if row is None or not password_matches(payload.password, row["password_hash"]):
        LOGIN_FAILURES.setdefault(key, []).append(datetime.now(timezone.utc))
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    LOGIN_FAILURES.pop(key, None)
    token, _ = create_auth_session(row["id"])
    response = JSONResponse(
        {
            "user": {
                "id": row["id"],
                "username": row["username"],
                "is_admin": bool(row["is_admin"]),
            }
        }
    )
    set_session_cookie(response, request, token)
    return response


@app.post("/api/auth/logout")
def logout(request: Request) -> JSONResponse:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        connection = user_connection()
        try:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?",
                (session_hash(token),),
            )
            connection.commit()
        finally:
            connection.close()
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/auth/me")
def me(request: Request) -> dict:
    return request.state.user


def require_admin(request: Request) -> None:
    if not request.state.user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")


@app.get("/api/admin/users")
def admin_users(request: Request) -> list[dict]:
    require_admin(request)
    connection = user_connection()
    try:
        rows = connection.execute(
            """
            SELECT u.id, u.username, u.is_admin, u.created_at,
                   (SELECT COUNT(*) FROM attempts a WHERE a.user_id = u.id)
                       AS attempt_count,
                   (SELECT MAX(answered_at) FROM attempts a WHERE a.user_id = u.id)
                       AS last_active_at,
                   (SELECT COUNT(*) FROM wrong_book w
                    WHERE w.user_id = u.id AND w.resolved = 0)
                       AS wrong_book_count
            FROM users u
            ORDER BY u.id
            """
        ).fetchall()
        return [
            {
                **dict(row),
                "is_admin": bool(row["is_admin"]),
            }
            for row in rows
        ]
    finally:
        connection.close()


@app.get("/api/admin/invites")
def admin_invites(request: Request) -> list[dict]:
    require_admin(request)
    connection = user_connection()
    try:
        rows = connection.execute(
            """
            SELECT i.id, i.code_prefix, i.created_at, i.expires_at, i.used_at,
                   creator.username AS created_by_name,
                   used.username AS used_by_name
            FROM invite_codes i
            JOIN users creator ON creator.id = i.created_by
            LEFT JOIN users used ON used.id = i.used_by
            ORDER BY i.id DESC
            LIMIT 50
            """
        ).fetchall()
        now = datetime.now(timezone.utc)
        result = []
        for row in rows:
            if row["used_at"]:
                status = "used"
            elif datetime.fromisoformat(row["expires_at"]) <= now:
                status = "expired"
            else:
                status = "active"
            result.append({**dict(row), "status": status})
        return result
    finally:
        connection.close()


@app.post("/api/admin/invites")
def create_invite(payload: InviteCreateRequest, request: Request) -> dict:
    require_admin(request)
    code = f"kg_{secrets.token_urlsafe(18)}"
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=payload.expires_days)).isoformat(
        timespec="seconds"
    )
    connection = user_connection()
    try:
        cursor = connection.execute(
            """
            INSERT INTO invite_codes
                (code_hash, code_prefix, created_by, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_hash(code),
                code[:10],
                request.state.user["id"],
                now.isoformat(timespec="seconds"),
                expires_at,
            ),
        )
        connection.commit()
        return {
            "id": int(cursor.lastrowid),
            "code": code,
            "expires_at": expires_at,
        }
    finally:
        connection.close()


@app.post("/api/admin/invites/{invite_id}/revoke")
def revoke_invite(invite_id: int, request: Request) -> dict:
    require_admin(request)
    connection = user_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE invite_codes
            SET expires_at = ?
            WHERE id = ? AND used_at IS NULL
            """,
            (utc_now(), invite_id),
        )
        connection.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="邀请码不存在或已经使用")
        return {"ok": True}
    finally:
        connection.close()


@app.get("/api/filters")
def filters(
    request: Request,
    subject: Literal["xingce", "shenlun"] = Query(default="xingce"),
) -> dict:
    connection = question_connection()
    try:
        module_rows = connection.execute(
            """
            SELECT module, COUNT(*) AS count
            FROM questions
            WHERE subject = ? AND module IS NOT NULL
            GROUP BY module
            ORDER BY count DESC, module
            """,
            (subject,),
        ).fetchall()
        region_rows = connection.execute(
            """
            SELECT p.region, COUNT(*) AS count
            FROM questions q
            JOIN papers p ON p.id = q.primary_paper_id
            WHERE q.subject = ? AND p.region IS NOT NULL
            GROUP BY p.region
            ORDER BY p.region
            """,
            (subject,),
        ).fetchall()
        year_rows = connection.execute(
            """
            SELECT p.year, COUNT(*) AS count
            FROM questions q
            JOIN papers p ON p.id = q.primary_paper_id
            WHERE q.subject = ? AND p.year IS NOT NULL
            GROUP BY p.year
            ORDER BY p.year DESC
            """,
            (subject,),
        ).fetchall()
        return {
            "modules": [dict(row) for row in module_rows],
            "regions": [dict(row) for row in region_rows],
            "years": [dict(row) for row in year_rows],
        }
    finally:
        connection.close()


@app.get("/api/papers")
def list_papers(
    request: Request,
    subject: Literal["xingce", "shenlun"] = Query(default="xingce"),
    module: str | None = None,
    region: str | None = None,
    year: int | None = None,
    query: str | None = None,
    limit: int = Query(default=100, ge=1, le=300),
) -> list[dict]:
    where = ["p.subject = ?"]
    params: list[object] = [subject]
    if region:
        where.append("p.region = ?")
        params.append(region)
    if year:
        where.append("p.year = ?")
        params.append(year)
    if module:
        where.append(
            """
            EXISTS (
                SELECT 1 FROM question_papers qp_filter
                JOIN questions q_filter ON q_filter.id = qp_filter.question_id
                WHERE qp_filter.paper_id = p.id
                  AND q_filter.module = ?
            )
            """
        )
        params.append(module)
    if query:
        where.append("p.title LIKE ?")
        params.append(f"%{query}%")
    params.append(limit)
    connection = question_connection()
    try:
        rows = connection.execute(
            f"""
            SELECT p.id, p.region, p.year, p.title, p.duration_seconds,
                   p.declared_question_count, COUNT(DISTINCT qp.question_id)
                       AS question_count
            FROM papers p
            LEFT JOIN question_papers qp ON qp.paper_id = p.id
            WHERE {' AND '.join(where)}
            GROUP BY p.id
            ORDER BY p.year DESC, p.region, p.title
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


@app.get("/api/papers/{paper_id}")
def paper_detail(paper_id: int, request: Request) -> dict:
    connection = question_connection()
    try:
        paper = connection.execute(
            """
            SELECT id, source_id, subject, region, year, title,
                   duration_seconds, declared_question_count, metadata_json
            FROM papers WHERE id = ?
            """,
            (paper_id,),
        ).fetchone()
        if paper is None:
            raise HTTPException(status_code=404, detail="试卷不存在")
        questions = connection.execute(
            """
            SELECT q.id, q.module, q.category,
                   substr(q.stem_text, 1, 180) AS preview
            FROM question_papers qp
            JOIN questions q ON q.id = qp.question_id
            WHERE qp.paper_id = ?
            ORDER BY COALESCE(qp.sequence, 999999), q.id
            """,
            (paper_id,),
        ).fetchall()
        result = dict(paper)
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        result["materials"] = fetch_materials(connection, paper_id)
        result["questions"] = [dict(row) for row in questions]
        return result
    finally:
        connection.close()


@app.get("/api/questions/{question_id}")
def question_detail(
    question_id: int,
    request: Request,
    reveal: bool = False,
) -> dict:
    connection = question_connection()
    try:
        return question_payload(connection, question_id, reveal=reveal)
    finally:
        connection.close()


@app.get("/api/practice")
def practice_question(
    request: Request,
    subject: Literal["xingce", "shenlun"] = Query(default="xingce"),
    module: str | None = None,
    region: str | None = None,
    year: int | None = None,
    exclude_id: int | None = None,
) -> dict:
    where = ["q.subject = ?", "q.source_id = ?"]
    params: list[object] = [
        subject,
        "gongkao-naoku" if subject == "xingce" else "errrc-kaogong-shuati",
    ]
    if subject == "xingce":
        where.append("q.answer_text IS NOT NULL")
    if module:
        where.append("q.module = ?")
        params.append(module)
    if region:
        where.append("p.region = ?")
        params.append(region)
    if year:
        where.append("p.year = ?")
        params.append(year)
    if exclude_id:
        where.append("q.id != ?")
        params.append(exclude_id)
    connection = question_connection()
    try:
        row = connection.execute(
            f"""
            SELECT q.id
            FROM questions q
            LEFT JOIN papers p ON p.id = q.primary_paper_id
            WHERE {' AND '.join(where)}
            ORDER BY RANDOM()
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="没有符合条件的新题")
        return question_payload(connection, row["id"], reveal=False)
    finally:
        connection.close()


@app.post("/api/questions/{question_id}/attempt")
def submit_attempt(
    question_id: int,
    payload: AttemptRequest,
    request: Request,
) -> dict:
    connection = question_connection()
    try:
        question = connection.execute(
            """
            SELECT id, question_type, answer_text, analysis_text
            FROM questions WHERE id = ?
            """,
            (question_id,),
        ).fetchone()
        if question is None:
            raise HTTPException(status_code=404, detail="题目不存在")
        selected = sorted({value.upper() for value in payload.selected_answer if value})
        is_correct, correct_keys = evaluate_answer(
            connection, question, selected, payload.free_text
        )
    finally:
        connection.close()

    user_id = request.state.user["id"]
    attempt_id = save_attempt(
        user_id=user_id,
        question_id=question_id,
        selected=selected,
        free_text=payload.free_text,
        is_correct=is_correct,
        elapsed_ms=payload.elapsed_ms,
    )
    record_wrong_book(user_id, question_id, is_correct)
    return {
        "attempt_id": attempt_id,
        "is_correct": is_correct,
        "selected_answer": selected,
        "correct_option_keys": correct_keys,
        "answer_text": question["answer_text"],
        "analysis_text": question["analysis_text"],
    }


def session_row_for_user(session_id: int, user_id: int) -> sqlite3.Row:
    connection = user_connection()
    try:
        row = connection.execute(
            """
            SELECT * FROM practice_sessions
            WHERE id = ? AND user_id = ?
            """,
            (session_id, user_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise HTTPException(status_code=404, detail="练习记录不存在")
    return row


@app.post("/api/practice-sessions")
def create_practice_session(
    payload: PracticeSessionRequest,
    request: Request,
) -> dict:
    connection = question_connection()
    try:
        paper = connection.execute(
            "SELECT * FROM papers WHERE id = ?",
            (payload.paper_id,),
        ).fetchone()
        if paper is None:
            raise HTTPException(status_code=404, detail="试卷不存在")
        questions = connection.execute(
            """
            SELECT q.id
            FROM question_papers qp
            JOIN questions q ON q.id = qp.question_id
            WHERE qp.paper_id = ?
            ORDER BY COALESCE(qp.sequence, 999999), q.id
            """,
            (payload.paper_id,),
        ).fetchall()
        if not questions:
            raise HTTPException(status_code=400, detail="试卷没有可用题目")
    finally:
        connection.close()

    question_ids = [int(row["id"]) for row in questions]
    duration = payload.duration_seconds or paper["duration_seconds"] or 7200
    if payload.mode == "practice":
        duration = None
    user_id = request.state.user["id"]
    connection = user_connection()
    try:
        cursor = connection.execute(
            """
            INSERT INTO practice_sessions
                (user_id, paper_id, mode, question_ids_json,
                 duration_seconds, started_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'active')
            """,
            (
                user_id,
                payload.paper_id,
                payload.mode,
                json.dumps(question_ids),
                duration,
                utc_now(),
            ),
        )
        connection.commit()
        session_id = int(cursor.lastrowid)
    finally:
        connection.close()
    return {
        "session_id": session_id,
        "paper": dict(paper),
        "mode": payload.mode,
        "question_count": len(question_ids),
        "duration_seconds": duration,
    }


@app.get("/api/practice-sessions")
def list_practice_sessions(request: Request) -> list[dict]:
    connection = user_connection()
    try:
        rows = connection.execute(
            """
            SELECT id, paper_id, mode, duration_seconds, started_at,
                   submitted_at, status,
                   json_array_length(question_ids_json) AS question_count
            FROM practice_sessions
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 30
            """,
            (request.state.user["id"],),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


@app.get("/api/practice-sessions/{session_id}")
def practice_session_detail(session_id: int, request: Request) -> dict:
    session = session_row_for_user(session_id, request.state.user["id"])
    question_ids = json.loads(session["question_ids_json"])
    connection = user_connection()
    try:
        answer_rows = connection.execute(
            """
            SELECT a.question_id, a.selected_answer, a.free_text
            FROM attempts a
            JOIN (
                SELECT question_id, MAX(id) AS max_id
                FROM attempts
                WHERE user_id = ? AND practice_session_id = ?
                GROUP BY question_id
            ) latest ON latest.max_id = a.id
            """,
            (request.state.user["id"], session_id),
        ).fetchall()
    finally:
        connection.close()
    answers = {
        row["question_id"]: {
            "selected_answer": json.loads(row["selected_answer"] or "[]"),
            "free_text": row["free_text"],
        }
        for row in answer_rows
    }
    question_connection_db = question_connection()
    try:
        meta = question_connection_db.execute(
            "SELECT id, title, region, year, subject FROM papers WHERE id = ?",
            (session["paper_id"],),
        ).fetchone()
        index = [
            {
                "position": position,
                "question_id": question_id,
                **answers.get(question_id, {}),
            }
            for position, question_id in enumerate(question_ids)
        ]
    finally:
        question_connection_db.close()
    return {
        **dict(session),
        "question_ids": question_ids,
        "paper": dict(meta) if meta else None,
        "questions": index,
        "answers": answers,
    }


@app.get("/api/practice-sessions/{session_id}/questions/{position}")
def session_question(session_id: int, position: int, request: Request) -> dict:
    session = session_row_for_user(session_id, request.state.user["id"])
    question_ids = json.loads(session["question_ids_json"])
    if position < 0 or position >= len(question_ids):
        raise HTTPException(status_code=404, detail="题号超出范围")
    connection = question_connection()
    try:
        reveal = session["status"] == "submitted"
        return question_payload(
            connection,
            int(question_ids[position]),
            reveal=reveal,
        )
    finally:
        connection.close()


@app.post("/api/practice-sessions/{session_id}/attempt")
def session_attempt(
    session_id: int,
    payload: dict,
    request: Request,
) -> dict:
    session = session_row_for_user(session_id, request.state.user["id"])
    if session["status"] != "active":
        raise HTTPException(status_code=409, detail="试卷已经提交")
    question_id = int(payload.get("question_id", 0))
    if question_id not in json.loads(session["question_ids_json"]):
        raise HTTPException(status_code=400, detail="题目不属于当前试卷")
    selected = sorted(
        {
            str(value).upper()
            for value in payload.get("selected_answer", [])
            if value
        }
    )
    free_text = str(payload.get("free_text", ""))
    elapsed_ms = payload.get("elapsed_ms")
    connection = question_connection()
    try:
        question = connection.execute(
            """
            SELECT id, question_type, answer_text, analysis_text
            FROM questions WHERE id = ?
            """,
            (question_id,),
        ).fetchone()
        is_correct, correct_keys = evaluate_answer(
            connection, question, selected, free_text
        )
    finally:
        connection.close()
    user_id = request.state.user["id"]
    attempt_id = save_attempt(
        user_id=user_id,
        question_id=question_id,
        selected=selected,
        free_text=free_text,
        is_correct=is_correct,
        elapsed_ms=elapsed_ms,
        practice_session_id=session_id,
    )
    record_wrong_book(user_id, question_id, is_correct)
    reveal = session["mode"] == "practice"
    return {
        "attempt_id": attempt_id,
        "saved": True,
        "is_correct": is_correct if reveal else None,
        "answer_text": question["answer_text"] if reveal else None,
        "correct_option_keys": correct_keys if reveal else None,
        "analysis_text": question["analysis_text"] if reveal else None,
    }


@app.post("/api/practice-sessions/{session_id}/submit")
def submit_practice_session(session_id: int, request: Request) -> dict:
    session = session_row_for_user(session_id, request.state.user["id"])
    if session["status"] == "submitted":
        raise HTTPException(status_code=409, detail="试卷已经提交")
    question_ids = json.loads(session["question_ids_json"])
    connection = user_connection()
    try:
        rows = connection.execute(
            """
            SELECT a.question_id, a.is_correct
            FROM attempts a
            JOIN (
                SELECT question_id, MAX(id) AS max_id
                FROM attempts
                WHERE user_id = ? AND practice_session_id = ?
                GROUP BY question_id
            ) latest ON latest.max_id = a.id
            """,
            (request.state.user["id"], session_id),
        ).fetchall()
        connection.execute(
            """
            UPDATE practice_sessions
            SET status = 'submitted', submitted_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (utc_now(), session_id, request.state.user["id"]),
        )
        connection.commit()
    finally:
        connection.close()
    answered = len(rows)
    graded = sum(row["is_correct"] is not None for row in rows)
    correct = sum(row["is_correct"] == 1 for row in rows)
    return {
        "session_id": session_id,
        "question_count": len(question_ids),
        "answered": answered,
        "graded": graded,
        "correct": correct,
        "accuracy": round(correct / graded, 4) if graded else None,
    }


@app.get("/api/attempts/summary")
def attempt_summary(request: Request) -> dict:
    connection = user_connection()
    try:
        row = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(is_correct), 0) AS correct,
                   COALESCE(SUM(is_correct IS NOT NULL), 0) AS graded
            FROM attempts
            WHERE user_id = ?
            """,
            (request.state.user["id"],),
        ).fetchone()
        wrong = connection.execute(
            """
            SELECT COUNT(*) FROM wrong_book
            WHERE user_id = ? AND resolved = 0
            """,
            (request.state.user["id"],),
        ).fetchone()[0]
        return {
            "total": row["total"],
            "correct": row["correct"],
            "graded": row["graded"],
            "wrong_book_count": wrong,
            "accuracy": (
                round(row["correct"] / row["graded"], 4) if row["graded"] else None
            ),
        }
    finally:
        connection.close()


@app.get("/api/wrongbook")
def wrong_book(
    request: Request,
    subject: Literal["xingce", "shenlun"] | None = None,
    limit: int = Query(default=100, ge=1, le=300),
) -> dict:
    user_connection_db = user_connection()
    try:
        wrong_rows = user_connection_db.execute(
            """
            SELECT question_id, wrong_count, correct_after_wrong, last_wrong_at
            FROM wrong_book
            WHERE user_id = ? AND resolved = 0
            ORDER BY last_wrong_at DESC
            LIMIT ?
            """,
            (request.state.user["id"], limit),
        ).fetchall()
    finally:
        user_connection_db.close()

    if not wrong_rows:
        return {"counts": {}, "items": []}

    question_ids = [row["question_id"] for row in wrong_rows]
    placeholders = ",".join("?" for _ in question_ids)
    where = [f"q.id IN ({placeholders})"]
    params: list[object] = list(question_ids)
    if subject:
        where.append("q.subject = ?")
        params.append(subject)
    connection = question_connection()
    try:
        rows = connection.execute(
            f"""
            SELECT q.id, q.subject, q.module, q.category,
                   substr(q.stem_text, 1, 220) AS preview,
                   p.title AS paper_title, p.region, p.year
            FROM questions q
            LEFT JOIN papers p ON p.id = q.primary_paper_id
            WHERE {' AND '.join(where)}
            """,
            params,
        ).fetchall()
        metadata = {row["question_id"]: row for row in wrong_rows}
        questions = {row["id"]: dict(row) for row in rows}
        items = []
        for question_id in question_ids:
            if question_id not in questions:
                continue
            item = questions[question_id]
            wrong = metadata[question_id]
            item.update(
                {
                    "wrong_count": wrong["wrong_count"],
                    "correct_after_wrong": wrong["correct_after_wrong"],
                    "last_wrong_at": wrong["last_wrong_at"],
                }
            )
            items.append(item)
        counts: dict[str, int] = {}
        for item in items:
            counts[item["subject"]] = counts.get(item["subject"], 0) + 1
        return {"counts": counts, "items": items}
    finally:
        connection.close()


@app.post("/api/wrongbook/{question_id}/resolve")
def resolve_wrong_question(question_id: int, request: Request) -> dict:
    connection = user_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE wrong_book SET resolved = 1
            WHERE user_id = ? AND question_id = ?
            """,
            (request.state.user["id"], question_id),
        )
        connection.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="错题记录不存在")
        return {"ok": True}
    finally:
        connection.close()


@app.get("/api/images/{source_id}/{asset_path:path}")
def image_file(source_id: str, asset_path: str) -> FileResponse:
    source_root = IMAGE_ROOTS.get(source_id)
    if source_root is None:
        raise HTTPException(status_code=404)
    candidate = (source_root / asset_path).resolve()
    if (
        source_root not in candidate.parents
        or candidate.suffix.lower() not in IMAGE_SUFFIXES
        or not candidate.is_file()
    ):
        raise HTTPException(status_code=404)
    return FileResponse(candidate, media_type=IMAGE_MEDIA_TYPES[candidate.suffix.lower()])


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
