"""
SH HOST - HTML Hosting Server
Run:  pip install -r requirements.txt && python app.py
Admin panel: /admin   (set ADMIN_USER / ADMIN_PASS / SECRET_KEY env vars in production)
"""
import os, re, io, json, time, shutil, sqlite3, secrets, zipfile
import urllib.request, urllib.error
from functools import wraps
from flask import (Flask, request, session, jsonify, render_template, send_file, send_from_directory,
                   abort, redirect, Response, g)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
SITES_DIR = os.path.join(DATA_DIR, "sites")
DB_PATH = os.path.join(DATA_DIR, "shhost.db")
os.makedirs(SITES_DIR, exist_ok=True)

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")      # CHANGE THIS
BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "ffx.qd.je")            # sites open as name.ffx.qd.je (also editable in admin Settings)

app = Flask(__name__, template_folder=BASE_DIR)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))   # https on Render

NAME_RE = re.compile(r"^[a-z0-9-]{2,40}$")
RESERVED = {"admin", "api", "login", "logout", "register", "upload", "static", "s", "www"}

DEFAULT_SETTINGS = {
    "site_name": "SH HOST",
    "tagline": "Hosting Server",
    "allow_register": "1",
    "maintenance": "0",
    "max_projects": "5",
    "max_file_kb": "2048",
    "max_versions": "10",
    "youtube": "https://youtube.com/@developershuhan6",
    "telegram": "https://t.me/auraffxofficial",
    "tiktok": "https://tiktok.com/@mrshuhan207",
    "announcement": "",
    "base_domain": BASE_DOMAIN,
    "max_repo_mb": "25",
}

# ---------------------------------------------------------------- database
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()

def init_db():
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        banned INTEGER DEFAULT 0,
        max_projects INTEGER,
        created REAL
    );
    CREATE TABLE IF NOT EXISTS projects(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        user_id INTEGER NOT NULL,
        disabled INTEGER DEFAULT 0,
        views INTEGER DEFAULT 0,
        created REAL,
        updated REAL,
        repo_url TEXT,
        repo_branch TEXT,
        repo_folder TEXT,
        synced REAL
    );
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    """)
    cols = [r[1] for r in c.execute("PRAGMA table_info(projects)")]
    for col, typ in (("repo_url", "TEXT"), ("repo_branch", "TEXT"), ("repo_folder", "TEXT"), ("synced", "REAL")):
        if col not in cols:
            c.execute(f"ALTER TABLE projects ADD COLUMN {col} {typ}")
    for k, v in DEFAULT_SETTINGS.items():
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    c.commit(); c.close()

def get_settings():
    rows = db().execute("SELECT key,value FROM settings").fetchall()
    s = dict(DEFAULT_SETTINGS); s.update({r["key"]: r["value"] for r in rows})
    return s

def public_settings():
    s = get_settings()
    keys = ["site_name", "tagline", "allow_register", "maintenance", "youtube",
            "telegram", "tiktok", "announcement", "base_domain"]
    return {k: s[k] for k in keys}

# ---------------------------------------------------------------- helpers
def clean_name(n):
    n = re.sub(r"\s+", "-", (n or "").strip().lower())
    return re.sub(r"[^a-z0-9-]", "", n)

def site_dir(name): return os.path.join(SITES_DIR, name)

def base_domain():
    return (get_settings().get("base_domain") or "").strip().lower()

def site_url(name):
    bd = base_domain()
    if bd:
        scheme = "https" if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https" else "http"
        return f"{scheme}://{name}.{bd}"
    return request.host_url.rstrip("/") + f"/s/{name}/"

def list_versions(name):
    d = os.path.join(site_dir(name), "versions")
    if not os.path.isdir(d): return []
    return sorted([f[:-5] for f in os.listdir(d) if f.endswith(".html")], reverse=True)

def current_user():
    uid = session.get("uid")
    if not uid: return None
    u = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u or u["banned"]:
        session.pop("uid", None); return None
    return u

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not current_user(): return Response("Not logged in", 401)
        return f(*a, **k)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("admin"): return jsonify(error="unauthorized"), 401
        return f(*a, **k)
    return w

def save_html(name, data: bytes):
    d = site_dir(name); os.makedirs(os.path.join(d, "versions"), exist_ok=True)
    cur = os.path.join(d, "index.html")
    if os.path.exists(cur):
        shutil.copy(cur, os.path.join(d, "versions", time.strftime("%Y%m%d-%H%M%S") + ".html"))
        vs = list_versions(name)
        for old in vs[int(get_settings()["max_versions"]):]:
            try: os.remove(os.path.join(d, "versions", old + ".html"))
            except OSError: pass
    with open(cur, "wb") as f: f.write(data)

def public_dir(name): return os.path.join(site_dir(name), "public")

def index_path(name):
    pub = public_dir(name)
    return os.path.join(pub, "index.html") if os.path.isdir(pub) else os.path.join(site_dir(name), "index.html")

def subdomain_of_host():
    bd = base_domain()
    host = request.host.split(":")[0].lower()
    if bd and host.endswith("." + bd):
        sub = host[: -len(bd) - 1]
        if sub and sub != "www" and "." not in sub: return sub
    return None

# ---------------------------------------------------------------- GitHub import
GH_RE = re.compile(r"^(?:https?://(?:www\.)?github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/tree/([^/]+)(?:/(.+?))?)?/?$")

def parse_repo(text):
    m = GH_RE.match((text or "").strip())
    if not m: raise ValueError("Invalid GitHub link. Use https://github.com/owner/repo")
    return m.group(1), m.group(2), m.group(3), (m.group(4) or "")

def _http_get(url, limit, token=None):
    h = {"User-Agent": "SH-HOST", "Accept": "*/*"}
    if token: h["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
        buf = io.BytesIO()
        while True:
            chunk = r.read(65536)
            if not chunk: break
            buf.write(chunk)
            if buf.tell() > limit: raise ValueError("Repository is too large.")
        return buf.getvalue()

def fetch_repo(owner, repo, branch, maxb):
    token = os.environ.get("GITHUB_TOKEN", "")
    tries = [branch] if branch else []
    if not branch:
        try:
            info = json.loads(_http_get(f"https://api.github.com/repos/{owner}/{repo}", 1_000_000, token or None))
            tries.append(info.get("default_branch") or "main")
        except Exception:
            pass
        tries += [b for b in ("main", "master") if b not in tries]
    for b in tries:
        url = (f"https://api.github.com/repos/{owner}/{repo}/zipball/{b}" if token
               else f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{b}")
        try:
            return _http_get(url, maxb * 2, token or None), b
        except urllib.error.HTTPError as e:
            if e.code in (403, 429): raise ValueError("GitHub rate limit reached. Try again later.")
            if e.code in (404, 401): continue
            raise ValueError(f"GitHub error {e.code}.")
        except urllib.error.URLError:
            raise ValueError("Could not reach GitHub.")
    raise ValueError("Repository or branch not found (repo must be public).")

def extract_site(zip_bytes, dest, folder, maxb):
    try: z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile: raise ValueError("Downloaded file is not a valid zip.")
    items = [i for i in z.infolist() if not i.is_dir()]
    if not items: raise ValueError("Repository is empty.")
    root = items[0].filename.split("/")[0] + "/"
    base = root + (folder.strip("/") + "/" if folder.strip("/") else "")
    files, total = [], 0
    for i in items:
        if not i.filename.startswith(base): continue
        rel = i.filename[len(base):]
        parts = rel.split("/")
        if "\\" in rel or any(p in ("", "..") or p.startswith(".") for p in parts): continue
        total += i.file_size
        if total > maxb: raise ValueError("Site files are too large.")
        files.append((i, rel))
        if len(files) > 3000: raise ValueError("Too many files (max 3000).")
    if not any(rel == "index.html" for _, rel in files):
        raise ValueError("index.html not found" + (" in that folder." if folder else " in repo root. Set the folder option if it is inside a subfolder."))
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    for i, rel in files:
        out = os.path.join(tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f: f.write(z.read(i))
    shutil.rmtree(dest, ignore_errors=True)
    os.rename(tmp, dest)

def pull_repo(name, owner, repo, branch, folder):
    maxb = int(get_settings()["max_repo_mb"]) * 1024 * 1024
    data, used = fetch_repo(owner, repo, branch, maxb)
    os.makedirs(site_dir(name), exist_ok=True)
    extract_site(data, public_dir(name), folder, maxb)
    return used

# ---------------------------------------------------------------- user pages
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/settings")
def api_settings():
    return jsonify(public_settings())

@app.route("/register", methods=["POST"])
def register():
    s = get_settings()
    if s["allow_register"] != "1": return Response("Registration is closed.", 403)
    d = request.get_json(silent=True) or {}
    u, p = (d.get("username") or "").strip(), d.get("password") or ""
    if not re.match(r"^[A-Za-z0-9_.-]{3,24}$", u): return Response("Username: 3-24 letters/numbers.", 400)
    if len(p) < 4: return Response("Password too short (min 4).", 400)
    try:
        db().execute("INSERT INTO users(username,password,created) VALUES(?,?,?)",
                     (u, generate_password_hash(p), time.time()))
        db().commit()
    except sqlite3.IntegrityError:
        return Response("Username already taken.", 409)
    return "ok"

@app.route("/login", methods=["POST"])
def login():
    if get_settings()["maintenance"] == "1": return Response("Server under maintenance.", 503)
    d = request.get_json(silent=True) or {}
    u = db().execute("SELECT * FROM users WHERE username=?", ((d.get("username") or "").strip(),)).fetchone()
    if not u or not check_password_hash(u["password"], d.get("password") or ""):
        return Response("Wrong username or password.", 401)
    if u["banned"]: return Response("Your account is banned.", 403)
    session["uid"] = u["id"]; session.permanent = d.get("remember") == "yes"
    return "ok"

@app.route("/logout")
def logout():
    session.pop("uid", None); return "ok"

def project_view(r):
    return {"name": r["name"], "url": site_url(r["name"]), "versions": list_versions(r["name"]),
            "views": r["views"], "disabled": bool(r["disabled"]),
            "repo_url": r["repo_url"], "repo_branch": r["repo_branch"], "synced": r["synced"]}

@app.route("/api/user")
@login_required
def api_user():
    u = current_user()
    rows = db().execute("SELECT * FROM projects WHERE user_id=? ORDER BY created DESC", (u["id"],)).fetchall()
    return jsonify(username=u["username"], projects=[project_view(r) for r in rows])

@app.route("/upload", methods=["POST"])
@login_required
def upload():
    u, s = current_user(), get_settings()
    f = request.files.get("siteFile")
    is_update = request.form.get("isUpdate") == "true"
    name = clean_name(request.form.get("siteName"))
    if not f or not re.search(r"\.html?$", f.filename or "", re.I): return Response("Only .html/.htm files.", 400)
    data = f.read()
    if len(data) > int(s["max_file_kb"]) * 1024: return Response(f"File too large (max {s['max_file_kb']} KB).", 413)
    if is_update:
        p = db().execute("SELECT * FROM projects WHERE name=? AND user_id=?", (name, u["id"])).fetchone()
        if not p: return Response("Project not found.", 404)
        if os.path.isdir(public_dir(name)):
            shutil.rmtree(public_dir(name), ignore_errors=True)
            db().execute("UPDATE projects SET repo_url=NULL, repo_branch=NULL, repo_folder=NULL WHERE id=?", (p["id"],))
        save_html(name, data)
        db().execute("UPDATE projects SET updated=? WHERE id=?", (time.time(), p["id"])); db().commit()
        return "ok"
    if not NAME_RE.match(name) or name in RESERVED: return Response("Invalid site name (2-40, a-z 0-9 -).", 400)
    if db().execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone(): return Response("Name already taken.", 409)
    limit = u["max_projects"] if u["max_projects"] is not None else int(s["max_projects"])
    cnt = db().execute("SELECT COUNT(*) c FROM projects WHERE user_id=?", (u["id"],)).fetchone()["c"]
    if cnt >= limit: return Response(f"Project limit reached ({limit}).", 403)
    save_html(name, data)
    now = time.time()
    db().execute("INSERT INTO projects(name,user_id,created,updated) VALUES(?,?,?,?)", (name, u["id"], now, now))
    db().commit()
    return "ok"

@app.route("/api/github-import", methods=["POST"])
@login_required
def github_import():
    u, s = current_user(), get_settings()
    d = request.get_json(silent=True) or {}
    name = clean_name(d.get("siteName"))
    try:
        owner, repo, b2, f2 = parse_repo(d.get("repoUrl"))
    except ValueError as e:
        return Response(str(e), 400)
    branch = (d.get("branch") or "").strip() or b2 or ""
    folder = (d.get("folder") or "").strip().strip("/") or f2
    if not NAME_RE.match(name) or name in RESERVED: return Response("Invalid site name (2-40, a-z 0-9 -).", 400)
    if db().execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone(): return Response("Name already taken.", 409)
    limit = u["max_projects"] if u["max_projects"] is not None else int(s["max_projects"])
    if db().execute("SELECT COUNT(*) c FROM projects WHERE user_id=?", (u["id"],)).fetchone()["c"] >= limit:
        return Response(f"Project limit reached ({limit}).", 403)
    try:
        used = pull_repo(name, owner, repo, branch, folder)
    except ValueError as e:
        shutil.rmtree(site_dir(name), ignore_errors=True)
        return Response(str(e), 400)
    now = time.time()
    db().execute("INSERT INTO projects(name,user_id,created,updated,repo_url,repo_branch,repo_folder,synced) VALUES(?,?,?,?,?,?,?,?)",
                 (name, u["id"], now, now, f"https://github.com/{owner}/{repo}", used, folder, now))
    db().commit()
    return "ok"

def sync_project(p):
    owner, repo, _, _ = parse_repo(p["repo_url"])
    used = pull_repo(p["name"], owner, repo, p["repo_branch"] or "", p["repo_folder"] or "")
    db().execute("UPDATE projects SET updated=?, synced=?, repo_branch=? WHERE id=?", (time.time(), time.time(), used, p["id"]))
    db().commit()

@app.route("/api/github-sync", methods=["POST"])
@login_required
def github_sync():
    name = (request.get_json(silent=True) or {}).get("siteName", "")
    p = db().execute("SELECT * FROM projects WHERE name=? AND user_id=?", (name, current_user()["id"])).fetchone()
    if not p or not p["repo_url"]: return Response("This project is not linked to GitHub.", 404)
    try: sync_project(p)
    except ValueError as e: return Response(str(e), 400)
    return "ok"

@app.route("/api/delete", methods=["POST"])
@login_required
def api_delete():
    name = (request.get_json(silent=True) or {}).get("siteName", "")
    p = db().execute("SELECT * FROM projects WHERE name=? AND user_id=?", (name, current_user()["id"])).fetchone()
    if not p: return Response("Not found.", 404)
    shutil.rmtree(site_dir(name), ignore_errors=True)
    db().execute("DELETE FROM projects WHERE id=?", (p["id"],)); db().commit()
    return "ok"

def do_rename(old, new, user_id=None):
    new = clean_name(new)
    if not NAME_RE.match(new) or new in RESERVED: return Response("Invalid new name.", 400)
    q = "SELECT * FROM projects WHERE name=?" + (" AND user_id=?" if user_id else "")
    p = db().execute(q, (old, user_id) if user_id else (old,)).fetchone()
    if not p: return Response("Project not found.", 404)
    if db().execute("SELECT 1 FROM projects WHERE name=?", (new,)).fetchone(): return Response("Name already taken.", 409)
    os.rename(site_dir(old), site_dir(new))
    db().execute("UPDATE projects SET name=? WHERE id=?", (new, p["id"])); db().commit()
    return "ok"

@app.route("/api/rename-project", methods=["POST"])
@login_required
def api_rename():
    d = request.get_json(silent=True) or {}
    return do_rename(d.get("oldName", ""), d.get("newName", ""), current_user()["id"])

@app.route("/api/download-version")
@login_required
def download_version():
    name, v = request.args.get("project", ""), request.args.get("v", "")
    if not db().execute("SELECT 1 FROM projects WHERE name=? AND user_id=?", (name, current_user()["id"])).fetchone(): abort(404)
    if not re.match(r"^[0-9-]+$", v): abort(400)
    path = os.path.join(site_dir(name), "versions", v + ".html")
    if not os.path.exists(path): abort(404)
    return send_file(path, as_attachment=True, download_name=f"{name}-{v}.html")

# ---------------------------------------------------------------- site serving
@app.before_request
def subdomain_router():
    sub = subdomain_of_host()
    if sub: return serve_site(sub, request.path.lstrip("/"))

@app.route("/s/<name>")
def site_redirect(name):
    return redirect(f"/s/{name}/")

@app.route("/s/<name>/", defaults={"p": ""})
@app.route("/s/<name>/<path:p>")
def serve_site(name, p=""):
    row = db().execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
    if not row: abort(404)
    if row["disabled"]: return Response("This site has been disabled by the administrator.", 403)
    pub = public_dir(name)
    if os.path.isdir(pub):
        rel = p or "index.html"
        if os.path.isdir(os.path.join(pub, rel)): rel = rel.rstrip("/") + "/index.html"
        resp = send_from_directory(pub, rel)
    else:
        if p not in ("", "index.html"): abort(404)
        path = os.path.join(site_dir(name), "index.html")
        if not os.path.exists(path): abort(404)
        resp = send_file(path, mimetype="text/html")
    if p in ("", "index.html"):
        db().execute("UPDATE projects SET views=views+1 WHERE id=?", (row["id"],)); db().commit()
    resp.headers["X-Content-Type-Options"] = "nosniff"
    if request.path.startswith("/s/"):   # path mode shares an origin with the panel -> sandbox it
        resp.headers["Content-Security-Policy"] = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
    return resp

# ---------------------------------------------------------------- admin
@app.route("/admin")
def admin_page():
    return render_template("admin.html", logged=bool(session.get("admin")))

@app.route("/admin/login", methods=["POST"])
def admin_login():
    d = request.get_json(silent=True) or {}
    ok = secrets.compare_digest(str(d.get("username", "")), ADMIN_USER) and \
         secrets.compare_digest(str(d.get("password", "")), ADMIN_PASS)
    if not ok:
        time.sleep(1); return jsonify(error="Wrong admin credentials"), 401
    session["admin"] = True
    return jsonify(ok=True)

@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin", None); return jsonify(ok=True)

@app.route("/admin/api/stats")
@admin_required
def admin_stats():
    q = lambda sql: db().execute(sql).fetchone()[0]
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(SITES_DIR) for f in fs)
    return jsonify(users=q("SELECT COUNT(*) FROM users"), banned=q("SELECT COUNT(*) FROM users WHERE banned=1"),
                   projects=q("SELECT COUNT(*) FROM projects"), disabled=q("SELECT COUNT(*) FROM projects WHERE disabled=1"),
                   views=q("SELECT COALESCE(SUM(views),0) FROM projects"), storage_kb=round(size / 1024, 1))

@app.route("/admin/api/users")
@admin_required
def admin_users():
    rows = db().execute("""SELECT u.id,u.username,u.banned,u.max_projects,u.created,
        (SELECT COUNT(*) FROM projects p WHERE p.user_id=u.id) AS projects FROM users u ORDER BY u.id DESC""").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/admin/api/user/<int:uid>", methods=["POST", "DELETE"])
@admin_required
def admin_user(uid):
    if request.method == "DELETE":
        for p in db().execute("SELECT name FROM projects WHERE user_id=?", (uid,)).fetchall():
            shutil.rmtree(site_dir(p["name"]), ignore_errors=True)
        db().execute("DELETE FROM projects WHERE user_id=?", (uid,))
        db().execute("DELETE FROM users WHERE id=?", (uid,)); db().commit()
        return jsonify(ok=True)
    d = request.get_json(silent=True) or {}
    if "banned" in d: db().execute("UPDATE users SET banned=? WHERE id=?", (1 if d["banned"] else 0, uid))
    if "max_projects" in d:
        v = d["max_projects"]; db().execute("UPDATE users SET max_projects=? WHERE id=?", (int(v) if str(v).strip() != "" else None, uid))
    if d.get("password"):
        db().execute("UPDATE users SET password=? WHERE id=?", (generate_password_hash(d["password"]), uid))
    db().commit(); return jsonify(ok=True)

@app.route("/admin/api/projects")
@admin_required
def admin_projects():
    rows = db().execute("""SELECT p.*, u.username FROM projects p JOIN users u ON u.id=p.user_id ORDER BY p.created DESC""").fetchall()
    return jsonify([{"id": r["id"], "name": r["name"], "owner": r["username"], "disabled": bool(r["disabled"]),
                     "views": r["views"], "updated": r["updated"], "url": site_url(r["name"]),
                     "repo_url": r["repo_url"]} for r in rows])

@app.route("/admin/api/project/<int:pid>", methods=["POST", "DELETE"])
@admin_required
def admin_project(pid):
    p = db().execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not p: return jsonify(error="not found"), 404
    if request.method == "DELETE":
        shutil.rmtree(site_dir(p["name"]), ignore_errors=True)
        db().execute("DELETE FROM projects WHERE id=?", (pid,)); db().commit()
        return jsonify(ok=True)
    d = request.get_json(silent=True) or {}
    if "disabled" in d:
        db().execute("UPDATE projects SET disabled=? WHERE id=?", (1 if d["disabled"] else 0, pid)); db().commit()
    if d.get("rename"):
        r = do_rename(p["name"], d["rename"])
        if r != "ok" and getattr(r, "status_code", 200) != 200: return jsonify(error=r.get_data(as_text=True)), r.status_code
    return jsonify(ok=True)

@app.route("/admin/api/project/<int:pid>/source", methods=["GET", "POST"])
@admin_required
def admin_source(pid):
    p = db().execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not p: return jsonify(error="not found"), 404
    path = index_path(p["name"])
    if request.method == "GET":
        if not os.path.exists(path): return jsonify(error="index.html not found"), 404
        with open(path, encoding="utf-8", errors="replace") as f: return jsonify(html=f.read())
    html = (request.get_json(silent=True) or {}).get("html", "").encode("utf-8")
    if os.path.isdir(public_dir(p["name"])):
        with open(path, "wb") as f: f.write(html)
    else:
        save_html(p["name"], html)
    return jsonify(ok=True)

@app.route("/admin/api/project/<int:pid>/sync", methods=["POST"])
@admin_required
def admin_sync(pid):
    p = db().execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not p or not p["repo_url"]: return jsonify(error="not a GitHub project"), 404
    try: sync_project(p)
    except ValueError as e: return jsonify(error=str(e)), 400
    return jsonify(ok=True)

@app.route("/admin/api/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        d = request.get_json(silent=True) or {}
        for k in DEFAULT_SETTINGS:
            if k in d:
                v = str(d[k]).strip()
                if k == "base_domain":
                    v = re.sub(r"^https?://|^\*?\.|/.*$", "", v.lower())
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
        db().commit()
    return jsonify(get_settings())

@app.route("/admin/api/backup")
@admin_required
def admin_backup():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r, _, fs in os.walk(DATA_DIR):
            for f in fs:
                full = os.path.join(r, f); z.write(full, os.path.relpath(full, DATA_DIR))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"shhost-backup-{time.strftime('%Y%m%d')}.zip")

init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
