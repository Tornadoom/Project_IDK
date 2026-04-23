from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import zipfile
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
BACKUP_DIR = DATA_DIR / "backups"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "dashboard.db"
DEFAULT_INVITE_CODE_HASH = "a2659fe460ae1a08406a3b9afc7def37efb809df36927762def965070174570d"
INVITE_CODE_HASH = os.getenv("DASHBOARD_INVITE_CODE_HASH", DEFAULT_INVITE_CODE_HASH)
INVITE_CODE_RAW = os.getenv("DASHBOARD_INVITE_CODE")
BACKUP_INTERVAL_SECONDS = 12 * 60 * 60
SESSION_COOKIE = "dashboard_session"


def ensure_dirs() -> None:
    for path in (DATA_DIR, UPLOAD_DIR, BACKUP_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 8000")
    return conn


def init_db() -> None:
    ensure_dirs()
    with db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                nickname TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                avatar_path TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                item TEXT NOT NULL,
                due_date TEXT,
                due_time TEXT,
                link TEXT,
                notes TEXT,
                priority TEXT NOT NULL DEFAULT 'P2',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cart_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                product_name TEXT NOT NULL,
                image_path TEXT,
                agree_a INTEGER NOT NULL DEFAULT 0,
                agree_b INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                entity TEXT NOT NULL,
                entity_id INTEGER,
                details TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            );

            CREATE INDEX IF NOT EXISTS idx_todos_user_due ON todos(user_id, due_date, due_time);
            CREATE INDEX IF NOT EXISTS idx_cart_user_updated ON cart_items(user_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_logs_user_created ON activity_logs(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(token, expires_at);
            """
        )


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 180_000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, expected = stored.split("$", 2)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 180_000)
    return hmac.compare_digest(digest.hex(), expected)


def verify_invite_code(invite: str) -> bool:
    invite = (invite or "").strip()
    if INVITE_CODE_RAW:
        return hmac.compare_digest(invite, INVITE_CODE_RAW)
    digest = hashlib.sha256(invite.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, INVITE_CODE_HASH)


def log_activity(user_id: int | None, action: str, entity: str, entity_id: int | None, details: dict | None = None) -> None:
    payload = json.dumps(details or {}, ensure_ascii=False)
    created_at = now_iso()
    with db() as conn:
        conn.execute(
            "INSERT INTO activity_logs (user_id, action, entity, entity_id, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, action, entity, entity_id, payload, created_at),
        )
    line = json.dumps(
        {
            "created_at": created_at,
            "user_id": user_id,
            "action": action,
            "entity": entity,
            "entity_id": entity_id,
            "details": details or {},
        },
        ensure_ascii=False,
    )
    with (LOG_DIR / "activity.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def normalize_url(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, flags=re.I):
        value = "https://" + value
    return value


def save_data_url(data_url: str | None, subdir: str, prefix: str) -> str | None:
    if not data_url:
        return None
    match = re.match(r"^data:image/(png|jpeg|jpg|webp|gif);base64,(.+)$", data_url, flags=re.I | re.S)
    if not match:
        raise ValueError("仅支持 png、jpg、jpeg、webp、gif 图片")
    ext = "jpg" if match.group(1).lower() == "jpeg" else match.group(1).lower()
    raw = base64.b64decode(match.group(2), validate=True)
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError("图片不能超过 5MB")
    target_dir = UPLOAD_DIR / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{prefix}_{int(time.time())}_{secrets.token_hex(6)}.{ext}"
    target = target_dir / filename
    target.write_bytes(raw)
    return str(target.relative_to(UPLOAD_DIR)).replace("\\", "/")


def public_data_url(path_value: str | None) -> str | None:
    if not path_value:
        return None
    return "/uploads/" + quote(path_value)


def make_backup() -> Path:
    ensure_dirs()
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    target = BACKUP_DIR / f"dashboard_backup_{stamp}.db"
    if DB_PATH.exists():
        shutil.copy2(DB_PATH, target)
        log_activity(None, "backup", "database", None, {"file": str(target.relative_to(ROOT))})
    return target


def backup_worker() -> None:
    while True:
        time.sleep(BACKUP_INTERVAL_SECONDS)
        try:
            make_backup()
        except Exception as exc:
            with (LOG_DIR / "backup_errors.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{now_iso()} {exc}\n")


def row_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def get_user_from_token(token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    with db() as conn:
        row = conn.execute(
            """
            SELECT users.*
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token = ? AND sessions.expires_at > ?
            """,
            (token, now_iso()),
        ).fetchone()
    return row


def get_cart_labels(user_id: int) -> dict:
    labels = {"agree_a": "A", "agree_b": "B"}
    with db() as conn:
        row = conn.execute("SELECT value FROM user_settings WHERE user_id = ? AND key = 'cart_labels'", (user_id,)).fetchone()
    if row:
        try:
            saved = json.loads(row["value"])
            labels["agree_a"] = (saved.get("agree_a") or "A").strip()[:20] or "A"
            labels["agree_b"] = (saved.get("agree_b") or "B").strip()[:20] or "B"
        except json.JSONDecodeError:
            pass
    return labels


def save_cart_labels(user_id: int, labels: dict) -> dict:
    cleaned = {
        "agree_a": (labels.get("agree_a") or "A").strip()[:20] or "A",
        "agree_b": (labels.get("agree_b") or "B").strip()[:20] or "B",
    }
    with db() as conn:
        conn.execute(
            """
            INSERT INTO user_settings (user_id, key, value, updated_at)
            VALUES (?, 'cart_labels', ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (user_id, json.dumps(cleaned, ensure_ascii=False), now_iso()),
        )
    return cleaned


def generate_markdown(user_id: int) -> str:
    labels = get_cart_labels(user_id)
    with db() as conn:
        todos = conn.execute(
            "SELECT * FROM todos WHERE user_id = ? ORDER BY COALESCE(due_date, '9999-12-31'), COALESCE(due_time, '23:59')",
            (user_id,),
        ).fetchall()
        carts = conn.execute("SELECT * FROM cart_items WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)).fetchall()
    lines = ["# Dashboard Export", "", f"导出时间：{now_iso()}", "", "## 待办事项", ""]
    lines.append("|事项|截止日期|截止时间|链接|紧急度|备注|")
    lines.append("|---|---|---|---|---|---|")
    for item in todos:
        link = item["link"] or ""
        link_md = f"[打开链接]({link})" if link else ""
        lines.append(
            f"|{item['item']}|{item['due_date'] or ''}|{item['due_time'] or ''}|{link_md}|{item['priority']}|{(item['notes'] or '').replace('|', '/') }|"
        )
    lines.extend(["", "## 购物车", "", f"|商品名|{labels['agree_a']}同意|{labels['agree_b']}同意|状态|图片|", "|---|---|---|---|---|"])
    for item in carts:
        status = "待购买" if item["agree_a"] and item["agree_b"] else "待确认"
        lines.append(
            f"|{item['product_name']}|{'是' if item['agree_a'] else '否'}|{'是' if item['agree_b'] else '否'}|{status}|{item['image_path'] or ''}|"
        )
    return "\n".join(lines) + "\n"


def xlsx_sheet_xml(name: str, rows: list[list[str]]) -> str:
    def cell(col: int, row_idx: int, value: str) -> str:
        letters = ""
        number = col + 1
        while number:
            number, rem = divmod(number - 1, 26)
            letters = chr(65 + rem) + letters
        return f'<c r="{letters}{row_idx}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'

    row_xml = []
    for idx, row in enumerate(rows, 1):
        row_xml.append(f'<row r="{idx}">' + "".join(cell(col, idx, value) for col, value in enumerate(row)) + "</row>")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        + "".join(row_xml)
        + "</sheetData></worksheet>"
    )


def generate_xlsx(user_id: int) -> bytes:
    labels = get_cart_labels(user_id)
    with db() as conn:
        todos = conn.execute(
            "SELECT * FROM todos WHERE user_id = ? ORDER BY COALESCE(due_date, '9999-12-31'), COALESCE(due_time, '23:59')",
            (user_id,),
        ).fetchall()
        carts = conn.execute("SELECT * FROM cart_items WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)).fetchall()
    todo_rows = [["事项", "截止日期", "截止时间", "链接", "紧急度", "备注"]]
    todo_rows += [[r["item"], r["due_date"] or "", r["due_time"] or "", r["link"] or "", r["priority"], r["notes"] or ""] for r in todos]
    cart_rows = [["商品名", f"{labels['agree_a']}同意", f"{labels['agree_b']}同意", "状态", "图片"]]
    cart_rows += [
        [r["product_name"], "是" if r["agree_a"] else "否", "是" if r["agree_b"] else "否", "待购买" if r["agree_a"] and r["agree_b"] else "待确认", r["image_path"] or ""]
        for r in carts
    ]
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>""")
        zf.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>""")
        zf.writestr("xl/workbook.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="待办事项" sheetId="1" r:id="rId1"/><sheet name="购物车" sheetId="2" r:id="rId2"/></sheets></workbook>""")
        zf.writestr("xl/_rels/workbook.xml.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>""")
        zf.writestr("xl/worksheets/sheet1.xml", xlsx_sheet_xml("待办事项", todo_rows))
        zf.writestr("xl/worksheets/sheet2.xml", xlsx_sheet_xml("购物车", cart_rows))
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    server_version = "ProjectIDKDashboard/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api("GET", parsed)
            return
        if parsed.path.startswith("/uploads/"):
            self.serve_upload(parsed.path)
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        self.handle_api("POST", urlparse(self.path))

    def do_PUT(self) -> None:
        self.handle_api("PUT", urlparse(self.path))

    def do_DELETE(self) -> None:
        self.handle_api("DELETE", urlparse(self.path))

    def log_message(self, fmt: str, *args) -> None:
        if not self.path.startswith("/api/"):
            return
        with (LOG_DIR / "access.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()} {self.address_string()} {fmt % args}\n")

    def json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def send_json(self, data: dict | list, status: int = 200, headers: dict | None = None) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json({"error": message}, status)

    def current_user(self) -> sqlite3.Row | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        return get_user_from_token(morsel.value if morsel else None)

    def require_user(self) -> sqlite3.Row | None:
        user = self.current_user()
        if not user:
            self.send_error_json(HTTPStatus.UNAUTHORIZED, "请先登录")
            return None
        return user

    def handle_api(self, method: str, parsed) -> None:
        try:
            path = parsed.path
            if path == "/api/register" and method == "POST":
                return self.register()
            if path == "/api/login" and method == "POST":
                return self.login()
            if path == "/api/logout" and method == "POST":
                return self.logout()
            if path == "/api/me" and method == "GET":
                return self.me()
            if path == "/api/profile" and method == "PUT":
                return self.update_profile()
            if path == "/api/profile/avatar" and method == "POST":
                return self.update_avatar()
            if path == "/api/settings/cart-labels":
                if method == "GET":
                    return self.get_cart_label_settings()
                if method == "PUT":
                    return self.update_cart_label_settings()
            if path == "/api/todos":
                if method == "GET":
                    return self.list_todos()
                if method == "POST":
                    return self.create_todo()
            if re.match(r"^/api/todos/\d+$", path):
                todo_id = int(path.rsplit("/", 1)[1])
                if method == "PUT":
                    return self.update_todo(todo_id)
                if method == "DELETE":
                    return self.delete_todo(todo_id)
            if path == "/api/cart":
                if method == "GET":
                    return self.list_cart()
                if method == "POST":
                    return self.create_cart()
            if re.match(r"^/api/cart/\d+$", path):
                item_id = int(path.rsplit("/", 1)[1])
                if method == "PUT":
                    return self.update_cart(item_id)
                if method == "DELETE":
                    return self.delete_cart(item_id)
            if path == "/api/logs" and method == "GET":
                return self.list_logs()
            if path == "/api/backup" and method == "POST":
                user = self.require_user()
                if not user:
                    return
                target = make_backup()
                return self.send_json({"ok": True, "file": str(target.relative_to(ROOT))})
            if path == "/api/export" and method == "GET":
                return self.export(parsed)
            self.send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")
        except ValueError as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except json.JSONDecodeError:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "JSON 格式不正确")
        except Exception as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务器错误：{exc}")

    def register(self) -> None:
        body = self.json_body()
        username = (body.get("username") or "").strip()
        nickname = (body.get("nickname") or "").strip()
        password = body.get("password") or ""
        if not verify_invite_code(body.get("invite") or ""):
            raise ValueError("邀请码不正确")
        if not username or not nickname or not password:
            raise ValueError("用户名、昵称和密码都必须填写")
        if not re.match(r"^[A-Za-z0-9_]{3,32}$", username):
            raise ValueError("用户名仅支持 3-32 位英文、数字和下划线")
        with db() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO users (username, nickname, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (username, nickname, hash_password(password), now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ValueError("用户名已存在")
        log_activity(cur.lastrowid, "register", "user", cur.lastrowid, {"username": username})
        self.send_json({"ok": True})

    def login(self) -> None:
        body = self.json_body()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if not user or not verify_password(password, user["password_hash"]):
                raise ValueError("用户名或密码不正确")
            token = secrets.token_urlsafe(32)
            expires = (dt.datetime.now(dt.timezone.utc).astimezone() + dt.timedelta(days=14)).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, user["id"], now_iso(), expires),
            )
        log_activity(user["id"], "login", "user", user["id"], {})
        self.send_json({"ok": True}, headers={"Set-Cookie": f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=1209600"})

    def logout(self) -> None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        if morsel:
            with db() as conn:
                conn.execute("DELETE FROM sessions WHERE token = ?", (morsel.value,))
        self.send_json({"ok": True}, headers={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})

    def me(self) -> None:
        user = self.require_user()
        if not user:
            return
        self.send_json(
            {
                "id": user["id"],
                "username": user["username"],
                "nickname": user["nickname"],
                "avatar_url": public_data_url(user["avatar_path"]),
            }
        )

    def update_profile(self) -> None:
        user = self.require_user()
        if not user:
            return
        body = self.json_body()
        nickname = (body.get("nickname") or "").strip()
        if not nickname:
            raise ValueError("昵称不能为空")
        with db() as conn:
            conn.execute("UPDATE users SET nickname = ? WHERE id = ?", (nickname, user["id"]))
        log_activity(user["id"], "update", "profile", user["id"], {"nickname": nickname})
        self.send_json({"ok": True})

    def update_avatar(self) -> None:
        user = self.require_user()
        if not user:
            return
        body = self.json_body()
        path = save_data_url(body.get("image"), "avatars", f"user_{user['id']}")
        with db() as conn:
            conn.execute("UPDATE users SET avatar_path = ? WHERE id = ?", (path, user["id"]))
        log_activity(user["id"], "update_avatar", "profile", user["id"], {"avatar_path": path})
        self.send_json({"ok": True, "avatar_url": public_data_url(path)})

    def get_cart_label_settings(self) -> None:
        user = self.require_user()
        if not user:
            return
        self.send_json(get_cart_labels(user["id"]))

    def update_cart_label_settings(self) -> None:
        user = self.require_user()
        if not user:
            return
        labels = save_cart_labels(user["id"], self.json_body())
        log_activity(user["id"], "update", "cart_labels", user["id"], labels)
        self.send_json({"ok": True, **labels})

    def list_todos(self) -> None:
        user = self.require_user()
        if not user:
            return
        query = parse_qs(urlparse(self.path).query)
        order = "ORDER BY updated_at DESC"
        if query.get("sort", [""])[0] == "due":
            order = "ORDER BY COALESCE(due_date, '9999-12-31'), COALESCE(due_time, '23:59')"
        with db() as conn:
            rows = conn.execute(f"SELECT * FROM todos WHERE user_id = ? {order}", (user["id"],)).fetchall()
        self.send_json([row_dict(row) for row in rows])

    def create_todo(self) -> None:
        user = self.require_user()
        if not user:
            return
        body = self.todo_payload()
        created = now_iso()
        with db() as conn:
            cur = conn.execute(
                """
                INSERT INTO todos (user_id, item, due_date, due_time, link, notes, priority, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user["id"], body["item"], body["due_date"], body["due_time"], body["link"], body["notes"], body["priority"], created, created),
            )
        log_activity(user["id"], "create", "todo", cur.lastrowid, body)
        self.send_json({"ok": True, "id": cur.lastrowid})

    def update_todo(self, todo_id: int) -> None:
        user = self.require_user()
        if not user:
            return
        body = self.todo_payload()
        with db() as conn:
            cur = conn.execute(
                """
                UPDATE todos SET item = ?, due_date = ?, due_time = ?, link = ?, notes = ?, priority = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (body["item"], body["due_date"], body["due_time"], body["link"], body["notes"], body["priority"], now_iso(), todo_id, user["id"]),
            )
        if cur.rowcount == 0:
            raise ValueError("待办不存在")
        log_activity(user["id"], "update", "todo", todo_id, body)
        self.send_json({"ok": True})

    def delete_todo(self, todo_id: int) -> None:
        user = self.require_user()
        if not user:
            return
        with db() as conn:
            cur = conn.execute("DELETE FROM todos WHERE id = ? AND user_id = ?", (todo_id, user["id"]))
        if cur.rowcount == 0:
            raise ValueError("待办不存在")
        log_activity(user["id"], "delete", "todo", todo_id, {})
        self.send_json({"ok": True})

    def todo_payload(self) -> dict:
        body = self.json_body()
        item = (body.get("item") or "").strip()
        if not item:
            raise ValueError("事项不能为空")
        priority = body.get("priority") or "P2"
        if priority not in ("P0", "P1", "P2"):
            raise ValueError("紧急度不正确")
        due_time = (body.get("due_time") or "").strip()
        if due_time and not re.match(r"^\d{2}:(00|15|30|45)$", due_time):
            raise ValueError("截止时间必须精确到 15 分钟")
        return {
            "item": item,
            "due_date": (body.get("due_date") or "").strip(),
            "due_time": due_time,
            "link": normalize_url(body.get("link")),
            "notes": (body.get("notes") or "").strip(),
            "priority": priority,
        }

    def list_cart(self) -> None:
        user = self.require_user()
        if not user:
            return
        with db() as conn:
            rows = conn.execute("SELECT * FROM cart_items WHERE user_id = ? ORDER BY updated_at DESC", (user["id"],)).fetchall()
        payload = []
        for row in rows:
            item = row_dict(row)
            item["image_url"] = public_data_url(row["image_path"])
            item["status"] = "待购买" if row["agree_a"] and row["agree_b"] else "待确认"
            payload.append(item)
        self.send_json(payload)

    def create_cart(self) -> None:
        user = self.require_user()
        if not user:
            return
        body = self.cart_payload()
        image_path = save_data_url(body.pop("image"), "cart", f"cart_user_{user['id']}") if body.get("image") else None
        created = now_iso()
        with db() as conn:
            cur = conn.execute(
                """
                INSERT INTO cart_items (user_id, product_name, image_path, agree_a, agree_b, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user["id"], body["product_name"], image_path, body["agree_a"], body["agree_b"], created, created),
            )
        log_activity(user["id"], "create", "cart", cur.lastrowid, {**body, "image_path": image_path})
        self.send_json({"ok": True, "id": cur.lastrowid})

    def update_cart(self, item_id: int) -> None:
        user = self.require_user()
        if not user:
            return
        body = self.cart_payload()
        with db() as conn:
            current = conn.execute("SELECT * FROM cart_items WHERE id = ? AND user_id = ?", (item_id, user["id"])).fetchone()
            if not current:
                raise ValueError("商品不存在")
            image_path = current["image_path"]
            if body.get("image"):
                image_path = save_data_url(body.pop("image"), "cart", f"cart_user_{user['id']}")
            else:
                body.pop("image", None)
            conn.execute(
                """
                UPDATE cart_items SET product_name = ?, image_path = ?, agree_a = ?, agree_b = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (body["product_name"], image_path, body["agree_a"], body["agree_b"], now_iso(), item_id, user["id"]),
            )
        log_activity(user["id"], "update", "cart", item_id, {**body, "image_path": image_path})
        self.send_json({"ok": True})

    def delete_cart(self, item_id: int) -> None:
        user = self.require_user()
        if not user:
            return
        with db() as conn:
            cur = conn.execute("DELETE FROM cart_items WHERE id = ? AND user_id = ?", (item_id, user["id"]))
        if cur.rowcount == 0:
            raise ValueError("商品不存在")
        log_activity(user["id"], "delete", "cart", item_id, {})
        self.send_json({"ok": True})

    def cart_payload(self) -> dict:
        body = self.json_body()
        product_name = (body.get("product_name") or "").strip()
        if not product_name:
            raise ValueError("商品名不能为空")
        return {
            "product_name": product_name,
            "image": body.get("image"),
            "agree_a": 1 if body.get("agree_a") else 0,
            "agree_b": 1 if body.get("agree_b") else 0,
        }

    def list_logs(self) -> None:
        user = self.require_user()
        if not user:
            return
        query = parse_qs(urlparse(self.path).query)
        date_value = (query.get("date", [""])[0] or "").strip()
        where = "WHERE user_id = ? OR user_id IS NULL"
        params: list = [user["id"]]
        if date_value:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_value):
                raise ValueError("日志日期格式不正确")
            where = "WHERE (user_id = ? OR user_id IS NULL) AND substr(created_at, 1, 10) = ?"
            params.append(date_value)
        with db() as conn:
            rows = conn.execute(
                f"SELECT * FROM activity_logs {where} ORDER BY id DESC LIMIT 200",
                params,
            ).fetchall()
        self.send_json([row_dict(row) for row in rows])

    def export(self, parsed) -> None:
        user = self.require_user()
        if not user:
            return
        fmt = parse_qs(parsed.query).get("format", ["md"])[0]
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        if fmt == "xlsx":
            raw = generate_xlsx(user["id"])
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="dashboard_{stamp}.xlsx"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        raw = generate_markdown(user["id"]).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="dashboard_{stamp}.md"')
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def serve_upload(self, path: str) -> None:
        rel = path.removeprefix("/uploads/")
        target = (DATA_DIR / rel).resolve()
        if not str(target).startswith(str(UPLOAD_DIR.resolve())) or not target.exists():
            self.send_error(404)
            return
        self.serve_file(target)

    def serve_static(self, path: str) -> None:
        if path in ("", "/"):
            target = PUBLIC_DIR / "index.html"
        else:
            target = (PUBLIC_DIR / path.lstrip("/")).resolve()
            if not str(target).startswith(str(PUBLIC_DIR.resolve())):
                self.send_error(403)
                return
            if not target.exists():
                target = PUBLIC_DIR / "index.html"
        self.serve_file(target)

    def serve_file(self, target: Path) -> None:
        ctype = "application/octet-stream"
        suffix = target.suffix.lower()
        if suffix == ".html":
            ctype = "text/html; charset=utf-8"
        elif suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif suffix == ".js":
            ctype = "text/javascript; charset=utf-8"
        elif suffix == ".png":
            ctype = "image/png"
        elif suffix in (".jpg", ".jpeg"):
            ctype = "image/jpeg"
        elif suffix == ".webp":
            ctype = "image/webp"
        elif suffix == ".gif":
            ctype = "image/gif"
        raw = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    init_db()
    threading.Thread(target=backup_worker, daemon=True).start()
    class DashboardServer(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 128

    server = DashboardServer((args.host, args.port), Handler)
    print(f"Dashboard running at http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
