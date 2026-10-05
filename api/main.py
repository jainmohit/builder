import asyncio, json, os, re, secrets, shutil, subprocess, sys, threading, time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

load_dotenv()
from . import auth, db, moderation, versions
from .auth import current_user
from .publish import publish as publish_site

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
BASE_PORT = 5300

MAX_PROJECTS = int(os.getenv("MAX_PROJECTS", "3"))
MAX_RUNS_PER_DAY = int(os.getenv("MAX_RUNS_PER_DAY", "15"))
MAX_TASK_CHARS = 4000
RUN_TIMEOUT = int(os.getenv("RUN_TIMEOUT_SEC", "1800"))
PUBLISH_ENABLED = os.getenv("PUBLISH_ENABLED", "0") == "1"
NSLOTS = int(os.getenv("LLM_SLOTS", "1"))
DAILY_TOKEN_BUDGET = int(os.getenv("DAILY_TOKEN_BUDGET", "600000"))
GLOBAL_TOKEN_BUDGET = int(os.getenv("GLOBAL_DAILY_TOKEN_BUDGET", "5000000"))
MAX_RUN_TOKENS = int(os.getenv("MAX_RUN_TOKENS", "300000"))

RUNS: dict[str, dict] = {}     # run_id -> {"lines", "done", "ok", "user", "project", "tokens"}
BUSY: dict[int, str] = {}      # user_id -> run_id (one active job per user)
TASKS: set = set()
SLOTS = asyncio.Semaphore(NSLOTS)
WAITING = {"n": 0}
PORT_LOCK = threading.Lock()


def sh(*cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def container_state(pid: str):
    r = sh("docker", "inspect", "-f", "{{.State.Running}}", f"proj-{pid}")
    return None if r.returncode != 0 else r.stdout.strip() == "true"


def ensure_running(pid: str, port: int):
    state = container_state(pid)
    if state is True:
        return
    if state is False:
        sh("docker", "start", f"proj-{pid}")
    else:
        sh(str(ROOT / "scripts" / "start_project.sh"), pid, str(port))


def snapshot(pid: str, label: str):
    d = ROOT / "projects" / pid
    versions.git(d, "add", "-A")
    versions.git(d, "commit", "-qm", label)


def csrf_guard(request: Request):
    # Browsers always send Origin on cross-origin POST/PATCH/DELETE. Reject any that isn't this site.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "cross-site request blocked")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    for p in db.q("SELECT * FROM projects"):
        await asyncio.to_thread(ensure_running, p["id"], p["port"])
    yield


app = FastAPI(lifespan=lifespan, dependencies=[Depends(csrf_guard)])


def get_project(pid: str, user: dict) -> dict:
    rows = db.q("SELECT * FROM projects WHERE id=? AND user_id=?", (pid, user["id"]))
    if not rows:
        raise HTTPException(404, "unknown project")
    return rows[0]


def require_admin(user: dict = Depends(current_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(403, "admin only")
    return user


def start_job(user: dict, pid: str) -> str:
    if user["id"] in BUSY:
        raise HTTPException(409, "You already have a job running. Wait for it to finish.")
    if len(RUNS) > 200:                                   # keep memory bounded
        for k in [k for k, v in RUNS.items() if v["done"]][:100]:
            del RUNS[k]
    rid = secrets.token_hex(8)
    RUNS[rid] = {"lines": [], "done": False, "ok": False, "user": user["id"], "project": pid, "tokens": 0}
    BUSY[user["id"]] = rid
    return rid


def end_job(rid: str, ok: bool):
    run = RUNS[rid]
    run["ok"] = ok
    run["done"] = True
    BUSY.pop(run["user"], None)


def runs_today(uid: int) -> int:
    return db.q("SELECT COUNT(*) AS n FROM runs WHERE user_id=? AND created_at>?",
                (uid, time.time() - 86400))[0]["n"]


def project_count(uid: int) -> int:
    return db.q("SELECT COUNT(*) AS n FROM projects WHERE user_id=?", (uid,))[0]["n"]


def tokens_today(uid: int | None = None) -> int:
    since = time.time() - 86400
    if uid is None:
        rows = db.q("SELECT COALESCE(SUM(tokens),0) AS n FROM runs WHERE created_at>?", (since,))
    else:
        rows = db.q("SELECT COALESCE(SUM(tokens),0) AS n FROM runs WHERE user_id=? AND created_at>?", (uid, since))
    return rows[0]["n"]


def over_budget(user: dict, run: dict) -> str | None:
    if run["tokens"] > MAX_RUN_TOKENS:
        return f"This build used more than {MAX_RUN_TOKENS:,} tokens and was stopped."
    if not user["is_admin"] and tokens_today(user["id"]) >= DAILY_TOKEN_BUDGET:
        return "You have used your daily token allowance. This build was stopped."
    if tokens_today() >= GLOBAL_TOKEN_BUDGET:
        return "The service has hit its daily capacity. This build was stopped."
    return None


class Creds(BaseModel):
    email: str
    password: str
    code: str = ""


class NewProject(BaseModel):
    name: str


class Chat(BaseModel):
    message: str


class Restore(BaseModel):
    sha: str


class Rename(BaseModel):
    name: str


class Suspend(BaseModel):
    suspended: bool


# ---------- auth ----------
@app.post("/api/auth/register")
def api_register(body: Creds, response: Response):
    auth.create_session(response, auth.register(body.email, body.password, body.code))
    return {"ok": True}


@app.post("/api/auth/login")
def api_login(body: Creds, request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    auth.create_session(response, auth.login(body.email, body.password, ip))
    return {"ok": True}


@app.post("/api/auth/logout")
def api_logout(request: Request, response: Response):
    auth.logout(request, response)
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    admin = bool(user["is_admin"])
    return {
        "email": user["email"], "is_admin": admin,
        "runs_used": runs_today(user["id"]), "runs_max": None if admin else MAX_RUNS_PER_DAY,
        "projects_used": project_count(user["id"]), "projects_max": None if admin else MAX_PROJECTS,
        "tokens_used": tokens_today(user["id"]), "tokens_max": None if admin else DAILY_TOKEN_BUDGET,
        "publish_enabled": admin or PUBLISH_ENABLED,
    }


@app.get("/api/status")
def status(user: dict = Depends(current_user)):
    rid = BUSY.get(user["id"])
    return {"run_id": rid, "project": RUNS[rid]["project"] if rid else None}


# ---------- projects ----------
@app.get("/api/projects")
def list_projects(user: dict = Depends(current_user)):
    return db.q("SELECT id, name, port, url FROM projects WHERE user_id=? ORDER BY created_at DESC", (user["id"],))


@app.post("/api/projects")
def create_project(body: NewProject, user: dict = Depends(current_user)):
    name = body.name.strip()[:60] or "Untitled"
    if not user["is_admin"] and project_count(user["id"]) >= MAX_PROJECTS:
        raise HTTPException(429, f"Project limit reached ({MAX_PROJECTS}). Delete a project to make a new one.")
    with PORT_LOCK:
        pid = "p" + secrets.token_hex(4)
        used = [p["port"] for p in db.q("SELECT port FROM projects")]
        port = max(used + [BASE_PORT - 1]) + 1
        r = sh(str(ROOT / "scripts" / "new_project.sh"), pid, str(port), timeout=180)
        if r.returncode != 0:
            raise HTTPException(500, "project setup failed: " + (r.stderr or r.stdout)[-400:])
        db.x("INSERT INTO projects(id, name, port, created_at, user_id) VALUES(?,?,?,?,?)",
             (pid, name, port, time.time(), user["id"]))
    return {"id": pid, "name": name, "port": port, "url": None}


@app.patch("/api/projects/{pid}")
def rename(pid: str, body: Rename, user: dict = Depends(current_user)):
    get_project(pid, user)
    db.x("UPDATE projects SET name=? WHERE id=?", (body.name.strip()[:60] or "Untitled", pid))
    return {"ok": True}


@app.delete("/api/projects/{pid}")
async def delete_project(pid: str, user: dict = Depends(current_user)):
    proj = get_project(pid, user)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", pid):
        raise HTTPException(400, "bad project id")
    rid = start_job(user, pid)
    try:
        await asyncio.to_thread(sh, "docker", "rm", "-f", f"proj-{pid}")
        await asyncio.to_thread(sh, "docker", "volume", "rm", f"nm-{pid}")
        shutil.rmtree(ROOT / "projects" / pid, ignore_errors=True)
        (ROOT / "screenshots" / f"{pid}.png").unlink(missing_ok=True)
        db.x("DELETE FROM messages WHERE project_id=?", (pid,))
        db.x("UPDATE runs SET log='', task='' WHERE project_id=?", (pid,))   # keep rows so quotas can't be reset by deleting
        db.x("DELETE FROM projects WHERE id=?", (pid,))
    finally:
        end_job(rid, True)
    return {"ok": True, "site": proj.get("site")}


@app.get("/api/projects/{pid}/messages")
def messages(pid: str, user: dict = Depends(current_user)):
    get_project(pid, user)
    return db.q("SELECT role, content FROM messages WHERE project_id=? ORDER BY id", (pid,))


@app.get("/api/projects/{pid}/screenshot")
def screenshot(pid: str, user: dict = Depends(current_user)):
    get_project(pid, user)
    path = ROOT / "screenshots" / f"{pid}.png"
    if not path.is_file():
        raise HTTPException(404, "no screenshot yet")
    return FileResponse(path)


@app.get("/api/projects/{pid}/history")
def history(pid: str, user: dict = Depends(current_user)):
    get_project(pid, user)
    return versions.history(ROOT / "projects" / pid)


@app.post("/api/projects/{pid}/restore")
async def restore(pid: str, body: Restore, user: dict = Depends(current_user)):
    proj = get_project(pid, user)
    rid = start_job(user, pid)
    ok, result, error = False, "", None
    try:
        await asyncio.to_thread(ensure_running, pid, proj["port"])
        result = await asyncio.to_thread(versions.restore, ROOT / "projects" / pid, body.sha)
        try:
            await asyncio.to_thread(sh, "docker", "exec", "-w", "/app", f"proj-{pid}",
                                    "npm", "install", "--no-audit", "--no-fund", "--prefer-offline", timeout=180)
        except Exception:
            pass
        ok = True
    except (ValueError, RuntimeError) as e:
        error = str(e)
    finally:
        end_job(rid, ok)
    if error:
        raise HTTPException(400, error)
    if result == "restored":
        db.x("INSERT INTO messages(project_id, role, content, created_at) VALUES(?,?,?,?)",
             (pid, "assistant", f"Restored version {body.sha}.", time.time()))
    return {"result": result}


# ---------- chat runs ----------
async def execute(run_id: str, pid: str, task: str, port: int, user: dict):
    run = RUNS[run_id]
    code = 1
    waiting = False
    try:
        await asyncio.to_thread(ensure_running, pid, port)
        if SLOTS.locked():
            ahead = WAITING["n"] + NSLOTS
            run["lines"].append(f"Queued behind other builds ({ahead} ahead). Your run starts when a slot frees up.")
        WAITING["n"] += 1
        waiting = True
        async with SLOTS:
            WAITING["n"] -= 1
            waiting = False
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", "-m", "agent.main", pid, task,
                cwd=str(ROOT), limit=1 << 20, env={**os.environ, "METER_STDOUT": "1"},
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            stop = {"why": None}

            async def pump():
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").rstrip()
                    if line.startswith("@@USAGE "):          # metering line from the agent: never shown in the log
                        try:
                            u = json.loads(line[8:])
                            run["tokens"] += int(u.get("in", 0)) + int(u.get("out", 0))
                        except Exception:
                            continue
                        db.x("UPDATE runs SET tokens=? WHERE id=?", (run["tokens"], run_id))
                        why = over_budget(user, run)
                        if why and not stop["why"]:
                            stop["why"] = why
                            proc.kill()
                        continue
                    run["lines"].append(line)

            try:
                await asyncio.wait_for(pump(), RUN_TIMEOUT)
                code = await proc.wait()
                if stop["why"]:
                    run["lines"].append(f"ERROR: {stop['why']}")
                    await asyncio.to_thread(snapshot, pid, "FAILED: stopped (token limit)")
                    code = 125
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                run["lines"].append(f"ERROR: run stopped after {RUN_TIMEOUT // 60} minutes.")
                await asyncio.to_thread(snapshot, pid, "FAILED: stopped (time limit)")
                code = 124
    except Exception as e:
        run["lines"].append(f"ERROR: {e}")
    finally:
        if waiting:
            WAITING["n"] -= 1
        ok = code == 0
        lines = run["lines"]
        idx = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith("Done:")), None)
        if ok and idx is not None:
            summary = "\n".join(lines[idx:])[:1500].strip()
        else:
            summary = "The run did not finish successfully. Open the agent log for details."
        db.x("UPDATE runs SET status=?, log=?, tokens=? WHERE id=?",
             ("done" if ok else "failed", "\n".join(lines), run["tokens"], run_id))
        db.x("INSERT INTO messages(project_id, role, content, created_at) VALUES(?,?,?,?)",
             (pid, "assistant", summary, time.time()))
        end_job(run_id, ok)


@app.post("/api/projects/{pid}/chat")
async def chat(pid: str, body: Chat, user: dict = Depends(current_user)):
    proj = get_project(pid, user)
    task = body.message.strip()
    if not task:
        raise HTTPException(400, "empty message")
    if len(task) > MAX_TASK_CHARS:
        raise HTTPException(400, f"Message too long (max {MAX_TASK_CHARS} characters).")
    if user["id"] in BUSY:
        raise HTTPException(409, "You already have a job running. Wait for it to finish.")
    if not user["is_admin"] and runs_today(user["id"]) >= MAX_RUNS_PER_DAY:
        raise HTTPException(429, f"Daily limit reached ({MAX_RUNS_PER_DAY} builds per 24 hours). Try again later.")
    if not user["is_admin"] and tokens_today(user["id"]) >= DAILY_TOKEN_BUDGET:
        raise HTTPException(429, "Your daily token allowance is used up. It frees up as older builds pass 24 hours.")
    if tokens_today() >= GLOBAL_TOKEN_BUDGET:
        raise HTTPException(503, "The service is at its daily capacity. Please try again later.")

    allowed, category, _reason = await asyncio.to_thread(moderation.check_prompt, task)
    if not allowed:
        moderation.log_flag(user["id"], pid, "prompt", category, task)
        if moderation.maybe_suspend(user):
            raise HTTPException(403, "This account has been suspended after repeated policy violations.")
        raise HTTPException(400, "This request can't be built here because it may break the content rules.")

    rid = start_job(user, pid)
    now = time.time()
    db.x("INSERT INTO messages(project_id, role, content, created_at) VALUES(?,?,?,?)", (pid, "user", task, now))
    db.x("INSERT INTO runs(id, project_id, task, status, created_at, user_id) VALUES(?,?,?,?,?,?)",
         (rid, pid, task, "running", now, user["id"]))
    t = asyncio.create_task(execute(rid, pid, task, proj["port"], user))
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)
    return {"run_id": rid}


@app.get("/api/runs/{run_id}/stream")
async def stream(run_id: str, request: Request, user: dict = Depends(current_user)):
    run = RUNS.get(run_id)
    if not run or run["user"] != user["id"]:
        raise HTTPException(404, "unknown run")
    last = request.headers.get("last-event-id")
    start = int(last) + 1 if last and last.isdigit() else 0

    async def gen():
        i = start
        while True:
            done = run["done"]
            while i < len(run["lines"]):
                yield f"id: {i}\ndata: {json.dumps(run['lines'][i])}\n\n"
                i += 1
            if done:
                yield f"event: end\ndata: {json.dumps({'ok': run['ok']})}\n\n"
                return
            await asyncio.sleep(0.3)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- publish ----------
async def run_publish(rid: str, proj: dict, user: dict):
    run = RUNS[rid]
    ok, url = False, None
    try:
        await asyncio.to_thread(ensure_running, proj["id"], proj["port"])
        root = ROOT / "projects" / proj["id"]
        run["lines"].append("Running content checks...")
        flags = await asyncio.to_thread(moderation.scan_project, root)
        for f in flags:
            run["lines"].append(f"[{f['severity']}] {f['category']}: {f['detail']}")
            moderation.log_flag(user["id"], proj["id"], "publish", f["category"], f"{f['severity']}: {f['detail']}")
        blocked = [f for f in flags if f["severity"] == "block"]
        if blocked and not user["is_admin"]:
            run["lines"].append("ERROR: publishing is blocked until the site owner has reviewed these findings.")
        else:
            if blocked:
                run["lines"].append("(admin account: publishing despite the findings above)")
            ok, url = await publish_site(
                run, proj, root,
                lambda s: db.x("UPDATE projects SET site=? WHERE id=?", (s, proj["id"])))
    except Exception as e:
        run["lines"].append(f"ERROR: {e}")
    finally:
        if ok and url:
            db.x("UPDATE projects SET url=? WHERE id=?", (url, proj["id"]))
            note = f"Published: {url}"
        else:
            note = "Publishing failed or was blocked. Open the agent log for details."
        run["lines"].append(note)
        db.x("INSERT INTO messages(project_id, role, content, created_at) VALUES(?,?,?,?)",
             (proj["id"], "assistant", note, time.time()))
        end_job(rid, ok)


@app.post("/api/projects/{pid}/publish")
async def publish(pid: str, user: dict = Depends(current_user)):
    if not (user["is_admin"] or PUBLISH_ENABLED):
        raise HTTPException(403, "Publishing is not enabled for your account yet.")
    proj = get_project(pid, user)
    rid = start_job(user, pid)
    t = asyncio.create_task(run_publish(rid, proj, user))
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)
    return {"run_id": rid}


# ---------- admin ----------
@app.get("/api/admin/usage")
def admin_usage(admin: dict = Depends(require_admin)):
    since = time.time() - 86400
    users = db.q("SELECT u.id, u.email, u.suspended, COALESCE(SUM(r.tokens),0) AS tokens, COUNT(r.id) AS runs "
                 "FROM users u LEFT JOIN runs r ON r.user_id=u.id AND r.created_at>? "
                 "GROUP BY u.id ORDER BY tokens DESC", (since,))
    return {"total_tokens_24h": tokens_today(), "global_budget": GLOBAL_TOKEN_BUDGET, "users": users}


@app.get("/api/admin/flags")
def admin_flags(admin: dict = Depends(require_admin)):
    return db.q("SELECT f.id, f.created_at, u.email, f.project_id, f.kind, f.category, f.detail "
                "FROM flags f LEFT JOIN users u ON u.id=f.user_id ORDER BY f.id DESC LIMIT 100")


@app.post("/api/admin/users/{uid}/suspend")
def admin_suspend(uid: int, body: Suspend, admin: dict = Depends(require_admin)):
    if uid == admin["id"]:
        raise HTTPException(400, "you can't suspend yourself")
    db.x("UPDATE users SET suspended=? WHERE id=?", (1 if body.suspended else 0, uid))
    if body.suspended:
        db.x("DELETE FROM sessions WHERE user_id=?", (uid,))
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")