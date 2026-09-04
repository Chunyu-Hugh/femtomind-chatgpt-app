"""
Globus transfer backend for the FitPack workflow.

Moves files and directories between facility collections (JLab <-> NERSC)
on behalf of a user, using a refresh token so that no browser is needed at
run time. The browser is needed exactly twice per user: once to obtain the
refresh token, and again whenever a collection that has not been consented
to yet is touched.

The four failure modes seen in practice are kept distinct, because they need
four different responses and collapsing them into "transfer failed" hides
the only thing the user can act on:

    ConsentRequired   -> hand the user an authorization URL
    AuthenticationFailed / session
                      -> the identity is not in session (NERSC expires these)
    PermissionDenied / invalid_user
                      -> the account is not mapped on that collection; a
                         facility-side membership problem, retrying is useless
    connect timeout   -> the endpoint is down; retrying later is reasonable

Collections are addressed by UUID. The two verified to work are listed in
KNOWN_COLLECTIONS; anything else can be passed through by UUID.
"""
from __future__ import annotations

import json
import os
import posixpath
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    import globus_sdk
except ImportError as exc:  # pragma: no cover - surfaced at runtime with config help
    globus_sdk = None  # type: ignore[assignment]
    _GLOBUS_IMPORT_ERROR: Optional[Exception] = exc
else:
    _GLOBUS_IMPORT_ERROR = None


# Registered under the AmSC project as a Thick Client (public client, no secret).
# A native-app client id is not a secret and is safe to keep in the source.
DEFAULT_CLIENT_ID = "0a821b0a-a37b-4422-a925-d222f2197057"

TRANSFER_SCOPE = "urn:globus:auth:scope:transfer.api.globus.org:all"
LOGIN_SCOPES = [TRANSFER_SCOPE, "openid", "profile", "email"]

# Verified working 2026-09-02. Other collections at the same facilities either
# time out (jlab#scidtn1, NERSC Perlmutter) or refuse the identity mapping
# (the NERSC "jlab Collab" collections need membership in that NERSC project).
KNOWN_COLLECTIONS: Dict[str, str] = {
    "jlab": "b0fca1ad-f485-4a00-8fcd-bca0b93a2a1c",       # jlab#gw1
    "nersc": "9d6d994a-6d04-11e5-ba46-22000b92c6ec",      # NERSC DTN
}

# The workflow stores JLab paths as the cluster sees them; Globus exposes the
# same tree under a different root. Without this the LLM sends the path it has
# and gets a 404 every time.
JLAB_PATH_MAP = [
    ("/w/ceph24/JAM/", "/~/enp/work/JAM/"),
    ("/ceph24/JAM/", "/~/enp/work/JAM/"),
]


def globus_client_id() -> str:
    return os.environ.get("GLOBUS_CLIENT_ID", DEFAULT_CLIENT_ID).strip() or DEFAULT_CLIENT_ID


def _require_globus() -> None:
    if globus_sdk is None:
        raise GlobusNotConfigured(
            "the globus-sdk package is not installed; run "
            "`python -m pip install globus-sdk` in the environment running this server"
        ) from _GLOBUS_IMPORT_ERROR


# --------------------------------------------------------------------------
# Errors. Each one carries what the caller has to tell the user.
# --------------------------------------------------------------------------

class GlobusError(RuntimeError):
    """Base class for every Globus problem this module reports."""


class GlobusNotConfigured(GlobusError):
    """The SDK is missing, or this user has never logged in."""


class GlobusNeedsConsent(GlobusError):
    """A collection needs a one-time data_access grant.

    ``url`` is an authorization URL to hand to the user. After they approve it
    they get a code, which goes back through ``login_finish``.
    """

    def __init__(self, message: str, url: str, scopes: List[str]) -> None:
        super().__init__(message)
        self.url = url
        self.scopes = scopes


class GlobusNeedsSession(GlobusError):
    """An identity has to be re-authenticated.

    NERSC collections expire the session periodically; this is not a permanent
    failure and not a permissions problem.
    """

    def __init__(self, message: str, domains: List[str]) -> None:
        super().__init__(message)
        self.domains = domains


class GlobusPermissionDenied(GlobusError):
    """The account is not mapped on that collection. Retrying will not help."""


class GlobusEndpointUnavailable(GlobusError):
    """The collection's server did not answer. Retrying later is reasonable."""


class GlobusAlreadyInFlight(GlobusError):
    """The same paths are already being moved.

    Globus refuses a duplicate rather than copying twice, which is a useful
    answer and not really a failure: the right response is to poll the task
    that is already running, not to submit again.
    """


# --------------------------------------------------------------------------
# Token storage, one row per user.
# --------------------------------------------------------------------------

def ensure_token_table(conn: sqlite3.Connection) -> None:
    """Make sure the Globus tables exist, and that the database is not readable
    by everyone.

    The DDL itself lives in schema.py, which is shared with
    open-webui/routers/hpc.py so the two processes cannot drift. This only
    applies it, and applying it is idempotent.
    """
    from schema import apply_schema
    apply_schema(conn)
    conn.commit()   # apply_schema leaves transaction control to its caller
    _restrict_db_permissions(conn)


def _restrict_db_permissions(conn: sqlite3.Connection) -> None:
    """Take the group and world bits off the database file.

    Until Globus tokens were added this database held only usernames, paths
    and job metadata, and shipped world-readable. It now holds credentials,
    so the old mode is no longer acceptable.
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        path = row[2] if row else None
        if path and os.path.exists(path):
            mode = os.stat(path).st_mode & 0o777
            if mode & 0o077:
                os.chmod(path, mode & 0o700)
    except Exception:
        # Never let a permissions tweak break a transfer; the encryption
        # below is the load-bearing protection.
        pass


# --- Encryption at rest -----------------------------------------------------
#
# A Globus refresh token does not expire on its own and is enough by itself to
# read and write files at a facility as that user. Storing it as plain text in
# a SQLite file means a copy of that one file is a copy of everyone's access.
#
# The key is kept outside the database, so a leaked .db (a backup, a dump, a
# stray copy) is not enough on its own. It does not defend against someone who
# can read the whole directory; that needs a real secrets manager, and is worth
# revisiting if this is ever opened past a small group.

def _key_path() -> str:
    explicit = os.environ.get("GLOBUS_TOKEN_KEY_FILE")
    if explicit:
        return explicit
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".globus_token_key")


def _get_fernet() -> Any:
    from cryptography.fernet import Fernet

    env_key = os.environ.get("GLOBUS_TOKEN_KEY", "").strip()
    if env_key:
        return Fernet(env_key.encode())

    path = _key_path()
    if not os.path.exists(path):
        key = Fernet.generate_key()
        # Create with the restrictive mode rather than widening then narrowing.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
    else:
        os.chmod(path, 0o600)
        with open(path, "rb") as handle:
            key = handle.read().strip()
    return Fernet(key)


def _encrypt(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return _get_fernet().encrypt(value.encode()).decode()


def _decrypt(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    from cryptography.fernet import InvalidToken

    try:
        return _get_fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise GlobusNotConfigured(
            "the stored Globus credentials could not be decrypted; the key file "
            f"({_key_path()}) does not match the database. Log in again to replace them."
        ) from exc


@dataclass
class StoredTokens:
    refresh_token: str
    access_token: Optional[str] = None
    expires_at: Optional[int] = None
    scope: Optional[str] = None


def load_tokens(conn: sqlite3.Connection, user_id: str) -> Optional[StoredTokens]:
    ensure_token_table(conn)
    row = conn.execute(
        "SELECT refresh_token, access_token, expires_at, scope "
        "FROM user_globus_tokens WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        return None
    return StoredTokens(refresh_token=_decrypt(row[0]) or "",
                        access_token=_decrypt(row[1]),
                        expires_at=row[2], scope=row[3])


def save_tokens(conn: sqlite3.Connection, user_id: str, payload: Dict[str, Any]) -> None:
    """Store the transfer.api.globus.org entry of an ``by_resource_server`` dict."""
    ensure_token_table(conn)
    conn.execute(
        """
        INSERT INTO user_globus_tokens
            (user_id, refresh_token, access_token, expires_at, scope, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            refresh_token = excluded.refresh_token,
            access_token  = excluded.access_token,
            expires_at    = excluded.expires_at,
            scope         = excluded.scope,
            updated_at    = CURRENT_TIMESTAMP
        """,
        (
            user_id,
            _encrypt(payload["refresh_token"]),
            _encrypt(payload.get("access_token")),
            payload.get("expires_at_seconds"),
            payload.get("scope"),
        ),
    )
    conn.commit()


def forget_tokens(conn: sqlite3.Connection, user_id: str) -> bool:
    """Drop a user's stored credentials. Used to sign out, and to recover from
    a key that no longer matches the database."""
    ensure_token_table(conn)
    cur = conn.execute("DELETE FROM user_globus_tokens WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM globus_pending_logins WHERE user_id = ?", (user_id,))
    conn.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------
# Login. Two halves, because the middle step happens in the user's browser.
# --------------------------------------------------------------------------

# How long a half-finished login stays valid. Long enough to read an
# authorization page and copy a code, short enough that abandoned rows do not
# accumulate.
PENDING_LOGIN_TTL_SECONDS = 30 * 60


def _put_pending(conn: sqlite3.Connection, user_id: str,
                 verifier: str, scopes: List[str]) -> None:
    ensure_token_table(conn)
    conn.execute(
        """
        INSERT INTO globus_pending_logins (user_id, verifier, scopes, created_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            verifier   = excluded.verifier,
            scopes     = excluded.scopes,
            created_at = CURRENT_TIMESTAMP
        """,
        (user_id, verifier, json.dumps(scopes)),
    )
    conn.commit()


def _take_pending(conn: sqlite3.Connection, user_id: str) -> Optional[Dict[str, Any]]:
    """Fetch and delete the pending login, if it has not expired."""
    ensure_token_table(conn)
    conn.execute(
        "DELETE FROM globus_pending_logins "
        "WHERE created_at < datetime('now', ?)",
        (f"-{PENDING_LOGIN_TTL_SECONDS} seconds",),
    )
    row = conn.execute(
        "SELECT verifier, scopes FROM globus_pending_logins WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        conn.commit()
        return None
    conn.execute("DELETE FROM globus_pending_logins WHERE user_id = ?", (user_id,))
    conn.commit()
    return {"verifier": row[0], "scopes": json.loads(row[1])}


def login_start(conn: sqlite3.Connection, user_id: str,
                scopes: Optional[List[str]] = None) -> str:
    """Return an authorization URL for the user to open.

    ``scopes`` defaults to the base login scopes. It is also how a
    ``GlobusNeedsConsent`` is resolved: pass the scopes off that exception.

    The PKCE verifier goes to the database rather than to process memory,
    because the user's browser sits between this call and ``login_finish``
    and the two may not land on the same worker.
    """
    _require_globus()
    requested = scopes or LOGIN_SCOPES
    verifier = secrets.token_hex(32)
    client = globus_sdk.NativeAppAuthClient(globus_client_id())
    client.oauth2_start_flow(
        requested_scopes=requested,
        refresh_tokens=True,   # access_type=offline; this is what removes the browser later
        verifier=verifier,
    )
    _put_pending(conn, user_id, verifier, requested)
    return client.oauth2_get_authorize_url()


def login_finish(conn: sqlite3.Connection, user_id: str, code: str) -> Dict[str, Any]:
    """Exchange the code the user pasted back for tokens, and store them."""
    _require_globus()
    pending = _take_pending(conn, user_id)
    if not pending:
        raise GlobusNotConfigured(
            "no login is in progress for this user, or it expired; "
            "start one with globus_login_start and use the code within "
            f"{PENDING_LOGIN_TTL_SECONDS // 60} minutes"
        )
    client = globus_sdk.NativeAppAuthClient(globus_client_id())
    client.oauth2_start_flow(
        requested_scopes=pending["scopes"],
        refresh_tokens=True,
        verifier=pending["verifier"],
    )
    response = client.oauth2_exchange_code_for_tokens(code.strip())
    by_rs = response.by_resource_server
    transfer_tokens = by_rs.get("transfer.api.globus.org")
    if not transfer_tokens or not transfer_tokens.get("refresh_token"):
        raise GlobusNotConfigured(
            "the authorization did not return a transfer refresh token; "
            "make sure the consent screen was approved"
        )
    save_tokens(conn, user_id, transfer_tokens)
    return {
        "status": "ok",
        "scope": transfer_tokens.get("scope"),
        "expires_at": transfer_tokens.get("expires_at_seconds"),
    }


# --------------------------------------------------------------------------
# Client construction and error translation.
# --------------------------------------------------------------------------

def get_transfer_client(conn: sqlite3.Connection, user_id: str) -> Any:
    _require_globus()
    stored = load_tokens(conn, user_id)
    if stored is None:
        raise GlobusNotConfigured(
            "this user has not authorized Globus yet; call globus_login_start first"
        )
    auth_client = globus_sdk.NativeAppAuthClient(globus_client_id())
    authorizer = globus_sdk.RefreshTokenAuthorizer(
        stored.refresh_token,
        auth_client,
        access_token=stored.access_token,
        expires_at=stored.expires_at,
    )
    return globus_sdk.TransferClient(authorizer=authorizer)


def _translate_error(conn: sqlite3.Connection, user_id: str, exc: Exception) -> GlobusError:
    """Map a Globus API error onto the four things a user can actually do."""
    if globus_sdk is None or not isinstance(exc, globus_sdk.TransferAPIError):
        return GlobusError(str(exc))

    code = (exc.code or "").strip()
    message = (exc.message or str(exc)).strip()

    if code == "ConsentRequired":
        scopes = list(exc.info.consent_required.required_scopes or [])
        url = login_start(conn, user_id, scopes=scopes)
        return GlobusNeedsConsent(
            "This collection needs a one-time access grant. Open the "
            "authorization URL, approve it, and send back the code.",
            url=url,
            scopes=scopes,
        )

    lowered = message.lower()
    if code == "Conflict" or "has not yet completed" in lowered:
        return GlobusAlreadyInFlight(
            "The same paths are already being transferred. Globus will not "
            "copy them twice; check the transfer that is already running "
            "instead of submitting again."
        )

    if "reauthenticate" in lowered or "session" in lowered:
        domains: List[str] = []
        info = getattr(exc, "info", None)
        auth_req = getattr(info, "authorization_parameters", None)
        if auth_req is not None:
            raw = getattr(auth_req, "session_required_single_domain", None) or []
            domains = list(raw)
        return GlobusNeedsSession(
            "The facility requires you to re-authenticate this identity. "
            "NERSC expires these periodically; this is not a permissions problem.",
            domains=domains,
        )

    if exc.http_status == 403 or "permission" in lowered or "invalid_user" in lowered:
        return GlobusPermissionDenied(
            f"The collection refused this account: {message} "
            "This is a facility-side mapping or membership problem; retrying will not help."
        )

    if exc.http_status in (502, 503, 504) or "timed out" in lowered or "error (connect)" in lowered:
        return GlobusEndpointUnavailable(
            f"The collection did not respond: {message} "
            "The endpoint is likely down; retrying later is reasonable."
        )

    return GlobusError(f"{code or exc.http_status}: {message}")


def _call(conn: sqlite3.Connection, user_id: str, method: Any,
          *args: Any, **kwargs: Any) -> Any:
    try:
        return method(*args, **kwargs)
    except Exception as exc:  # narrowed inside _translate_error
        raise _translate_error(conn, user_id, exc) from exc


# globus-sdk 4 dropped the transfer client from TransferData's arguments.
# Both signatures are accepted so the module does not break on whichever
# version the deployment happens to have. Decided from the signature rather
# than by catching an error, because 3.x raises GlobusSDKUsageError rather
# than TypeError when called the 4.x way.
_TRANSFER_DATA_WANTS_CLIENT: Optional[bool] = None


def _new_transfer_data(tc: Any, src: str, dst: str, **kwargs: Any) -> Any:
    global _TRANSFER_DATA_WANTS_CLIENT
    if _TRANSFER_DATA_WANTS_CLIENT is None:
        import inspect
        params = inspect.signature(globus_sdk.TransferData.__init__).parameters
        _TRANSFER_DATA_WANTS_CLIENT = "transfer_client" in params
    if _TRANSFER_DATA_WANTS_CLIENT:
        return globus_sdk.TransferData(tc, src, dst, **kwargs)
    return globus_sdk.TransferData(src, dst, **kwargs)


# --------------------------------------------------------------------------
# Paths and collections.
# --------------------------------------------------------------------------

def resolve_collection(name_or_id: str) -> str:
    """Accept a short name from KNOWN_COLLECTIONS or a raw UUID."""
    key = (name_or_id or "").strip()
    return KNOWN_COLLECTIONS.get(key.lower(), key)


def to_globus_path(path: str, collection: str = "jlab") -> str:
    """Rewrite a cluster path into the form the Globus collection exposes."""
    if resolve_collection(collection) != KNOWN_COLLECTIONS["jlab"]:
        return path
    normalized = posixpath.normpath(path or "/")
    for local_prefix, globus_prefix in JLAB_PATH_MAP:
        if normalized.startswith(local_prefix):
            return globus_prefix + normalized[len(local_prefix):]
    return path


def to_local_path(path: str, collection: str = "jlab") -> str:
    """Inverse of :func:`to_globus_path`, for reporting back to the workflow."""
    if resolve_collection(collection) != KNOWN_COLLECTIONS["jlab"]:
        return path
    globus_prefix = JLAB_PATH_MAP[0][1]
    if path.startswith(globus_prefix):
        return JLAB_PATH_MAP[0][0] + path[len(globus_prefix):]
    return path


# --------------------------------------------------------------------------
# Operations.
# --------------------------------------------------------------------------

def list_remote(conn: sqlite3.Connection, user_id: str, collection: str,
                path: str, limit: int = 100) -> List[Dict[str, Any]]:
    tc = get_transfer_client(conn, user_id)
    cid = resolve_collection(collection)
    result = _call(conn, user_id, tc.operation_ls, cid, path=to_globus_path(path, collection))
    entries = []
    for entry in result:
        entries.append({
            "name": entry["name"],
            "type": entry["type"],
            "size": entry.get("size"),
            "last_modified": entry.get("last_modified"),
        })
        if len(entries) >= limit:
            break
    return entries


def _record_submission(conn: sqlite3.Connection, user_id: str, task_id: str,
                       chat_id: str, batch_id: str,
                       source_collection: str, source_path: str,
                       dest_collection: str, dest_path: str,
                       recursive: bool, label: str) -> None:
    conn.execute(
        """
        INSERT INTO globus_transfers
            (task_id, user_id, chat_id, batch_id,
             source_collection, source_path, dest_collection, dest_path,
             recursive, label, status, submitted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SUBMITTED', CURRENT_TIMESTAMP)
        ON CONFLICT(task_id) DO NOTHING
        """,
        (task_id, user_id, chat_id or "", batch_id or "",
         source_collection, source_path, dest_collection, dest_path,
         1 if recursive else 0, label or ""),
    )
    conn.commit()


def _record_status(conn: sqlite3.Connection, task_id: str, task: Dict[str, Any]) -> None:
    """Fold a polled task back into the row, so the table converges on the
    truth without anything polling in the background."""
    status = task.get("status")
    conn.execute(
        """
        UPDATE globus_transfers
           SET status            = ?,
               files_transferred = ?,
               bytes_transferred = ?,
               completed_at      = CASE WHEN ? IN ('SUCCEEDED', 'FAILED')
                                        THEN COALESCE(?, CURRENT_TIMESTAMP)
                                        ELSE completed_at END
         WHERE task_id = ?
        """,
        (status, task.get("files_transferred"), task.get("bytes_transferred"),
         status, task.get("completion_time"), task_id),
    )
    conn.commit()


def list_transfers(conn: sqlite3.Connection, user_id: str,
                   batch_id: Optional[str] = None,
                   chat_id: Optional[str] = None,
                   limit: int = 20) -> List[Dict[str, Any]]:
    """What this user has moved, most recent first.

    Filter by ``batch_id`` to answer "where did this campaign's results go",
    which is the question the table exists for.
    """
    ensure_token_table(conn)
    clauses = ["user_id = ?"]
    params: List[Any] = [user_id]
    if batch_id:
        clauses.append("batch_id = ?")
        params.append(batch_id)
    if chat_id:
        clauses.append("chat_id = ?")
        params.append(chat_id)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT task_id, batch_id, source_collection, source_path,
               dest_collection, dest_path, recursive, status,
               files_transferred, bytes_transferred, submitted_at, completed_at
          FROM globus_transfers
         WHERE {' AND '.join(clauses)}
         ORDER BY submitted_at DESC
         LIMIT ?
        """,
        params,
    ).fetchall()
    keys = ("task_id", "batch_id", "source_collection", "source_path",
            "dest_collection", "dest_path", "recursive", "status",
            "files_transferred", "bytes_transferred", "submitted_at",
            "completed_at")
    out = []
    for row in rows:
        item = dict(zip(keys, row))
        item["recursive"] = bool(item["recursive"])
        out.append(item)
    return out


def submit_transfer(conn: sqlite3.Connection, user_id: str,
                    source_collection: str, source_path: str,
                    dest_collection: str, dest_path: str,
                    recursive: bool = False,
                    label: Optional[str] = None,
                    chat_id: str = "",
                    batch_id: str = "") -> Dict[str, Any]:
    """Submit one file or one directory. Returns immediately with a task id.

    ``batch_id`` links the move back to the campaign whose output it is; it is
    what lets a finished campaign be asked where its results went.
    """
    tc = get_transfer_client(conn, user_id)
    src = resolve_collection(source_collection)
    dst = resolve_collection(dest_collection)
    task_label = label or f"femtomind-{int(time.time())}"
    data = _new_transfer_data(
        tc, src, dst,
        label=task_label,
        sync_level="checksum",
        verify_checksum=True,
    )
    data.add_item(
        to_globus_path(source_path, source_collection),
        to_globus_path(dest_path, dest_collection),
        recursive=recursive,
    )
    response = _call(conn, user_id, tc.submit_transfer, data)
    task_id = response["task_id"]
    _record_submission(conn, user_id, task_id, chat_id, batch_id,
                       source_collection, source_path,
                       dest_collection, dest_path,
                       recursive, task_label)
    return {
        "task_id": task_id,
        "source": f"{source_collection}:{source_path}",
        "destination": f"{dest_collection}:{dest_path}",
        "recursive": recursive,
        "batch_id": batch_id or None,
    }


def transfer_status(conn: sqlite3.Connection, user_id: str, task_id: str) -> Dict[str, Any]:
    tc = get_transfer_client(conn, user_id)
    task = _call(conn, user_id, tc.get_task, task_id)
    out = {
        "task_id": task_id,
        "status": task["status"],
        "files_transferred": task.get("files_transferred"),
        "bytes_transferred": task.get("bytes_transferred"),
        "request_time": task.get("request_time"),
        "completion_time": task.get("completion_time"),
    }
    if task["status"] == "FAILED":
        events = _call(conn, user_id, tc.task_event_list, task_id, limit=3)
        out["events"] = [
            {"code": e.get("code"), "description": e.get("description"),
             "details": e.get("details")}
            for e in events
        ]
    _record_status(conn, task_id, out)
    return out
