import subprocess, sys
from pathlib import Path
from .agent import run_agent
from .builder import build_site, is_fresh
from .tools import Sandbox
from .verify import verify

force_new = "--new" in sys.argv[1:]
args = [a for a in sys.argv[1:] if a != "--new"]
if len(args) < 2:
    sys.exit('usage: python -m agent.main <project_id> "<task>" [--new]')

project_id, task = args[0], " ".join(args[1:])
sb = Sandbox(project_id)
r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", f"proj-{project_id}"],
                   capture_output=True, text=True)
if r.returncode != 0 or r.stdout.strip() != "true":
    sys.exit(f"Container proj-{project_id} is not running. "
             f"Start it with: ./scripts/start_project.sh {project_id} <port>")

if force_new or is_fresh(project_id):
    print("Mode: build new site")
    build_site(project_id, task)
    print("Verifying generated site...")
    problems = verify(sb)
    if problems:
        report = "\n\n".join(problems)[:6000]
        print("Verification failed, handing over to the repair agent.")
        ok = run_agent(project_id,
                       "The site was just generated but verification failed. Fix these errors with minimal "
                       "changes. Do not redesign anything.\n\n" + report)
    else:
        print("Done: site generated and verified.")
        ok = True
else:
    print("Mode: edit existing site")
    ok = run_agent(project_id, task)

proj = Path(__file__).resolve().parent.parent / "projects" / project_id
msg = task[:60] if ok else "FAILED: " + task[:52]
subprocess.run(["git", "-C", str(proj), "add", "-A"], check=False)
subprocess.run(["git", "-C", str(proj), "commit", "-qm", msg], check=False)
sys.exit(0 if ok else 1)