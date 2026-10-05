import asyncio, re, shutil

PIPE, STDOUT, DEVNULL = asyncio.subprocess.PIPE, asyncio.subprocess.STDOUT, asyncio.subprocess.DEVNULL


async def stream_cmd(run, argv, cwd=None, timeout=600, echo=True):
    """Run a command, append its output to run['lines'] (if echo), return (exit_code, full_output)."""
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, stdin=DEVNULL, stdout=PIPE, stderr=STDOUT, limit=1 << 20)
    out: list[str] = []

    async def pump():
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            out.append(line)
            if echo:
                run["lines"].append(line)

    try:
        await asyncio.wait_for(pump(), timeout)
        code = await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        run["lines"].append(f"ERROR: timed out after {timeout}s")
        code = 124
    return code, "\n".join(out)


async def wrangler(run, args, cwd, timeout=600):
    code, out = await stream_cmd(run, ["wrangler", *args, "--force"], cwd=cwd, timeout=timeout)
    if code != 0 and re.search(r"unknown argument", out, re.I):
        run["lines"].append("(this wrangler has no --force flag, retrying without it)")
        code, out = await stream_cmd(run, ["wrangler", *args], cwd=cwd, timeout=timeout)
    return code, out


def site_name(name: str, pid: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30].strip("-") or "site"
    return f"{base}-{pid}"[:58].strip("-")


def production_url(url: str) -> str:
    # https://<hash>.<site>.pages.dev  ->  https://<site>.pages.dev
    m = re.fullmatch(r"https://([a-z0-9-]+)\.([a-z0-9-]+\.pages\.dev)", url)
    return f"https://{m.group(2)}" if m else url


async def publish(run, proj, root, save_site):
    """Returns (ok, url). Appends progress to run['lines']."""
    log = run["lines"].append
    pid = proj["id"]

    if not shutil.which("wrangler"):
        log("ERROR: wrangler is not installed. In a terminal run: npm install -g wrangler")
        return False, None
    code, out = await stream_cmd(run, ["wrangler", "whoami"], timeout=60, echo=False)   # not echoed: it prints your email
    if code != 0 or "not authenticated" in out.lower():
        log("ERROR: not logged in to Cloudflare. In a terminal run: wrangler login")
        return False, None

    log("Building the production bundle...")
    code, _ = await stream_cmd(
        run, ["docker", "exec", "-w", "/app", f"proj-{pid}", "npm", "run", "build"], timeout=300)
    if code != 0 or not (root / "dist" / "index.html").is_file():
        log("ERROR: the build failed. Ask the agent to fix the build errors, then publish again.")
        return False, None

    site = proj.get("site") or site_name(proj["name"], pid)
    if not proj.get("site"):
        log(f"Creating Cloudflare project {site}...")
        code, out = await wrangler(run, ["pages", "project", "create", site, "--production-branch", "main"], root, 120)
        if code != 0 and "already exists" not in out.lower():
            log("ERROR: could not create the Cloudflare project. See the log above.")
            return False, None
        save_site(site)

    log("Uploading dist/ ...")
    code, out = await wrangler(
        run, ["pages", "deploy", "dist", "--project-name", site, "--branch", "main", "--commit-dirty=true"],
        root, 600)
    if code != 0:
        log("ERROR: the upload failed. See the log above.")
        return False, None

    urls = re.findall(r"https://[a-z0-9.-]+\.(?:pages|workers)\.dev", out)
    url = production_url(urls[-1]) if urls else f"https://{site}.pages.dev"
    return True, url