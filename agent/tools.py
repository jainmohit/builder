import os, subprocess, shlex
from pathlib import Path

PROJECTS = Path(__file__).resolve().parent.parent / "projects"
IGNORE = {"node_modules", ".git", "dist"}
MAX_OUT = 6000        # chars of command output sent back to the model
MAX_FILE = 20000      # chars of a file sent back to the model


class Sandbox:
    def __init__(self, project_id: str):
        self.id = project_id
        self.root = (PROJECTS / project_id).resolve()
        self.container = f"proj-{project_id}"
        if not self.root.is_dir():
            raise SystemExit(f"No such project: {self.root}")

    def _safe(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root):
            raise ValueError("path escapes the project folder")
        parts = p.relative_to(self.root).parts
        if any(part in IGNORE for part in parts):
            raise ValueError("that path is not accessible")
        return p

    def list_files(self, path: str = ".") -> str:
        base = self._safe(path)
        out = []
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in IGNORE]
            for f in files:
                out.append(str(Path(dirpath, f).relative_to(self.root)))
        out.sort()
        return "\n".join(out[:200]) or "(empty)"

    def read_file(self, path: str) -> str:
        p = self._safe(path)
        if not p.is_file():
            return f"ERROR: {path} does not exist"
        text = p.read_text(errors="replace")
        if len(text) > MAX_FILE:
            text = text[:MAX_FILE] + "\n...[truncated]"
        return text

    def write_file(self, path: str, content: str) -> str:
        p = self._safe(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {len(content)} chars to {path}"

    def exec(self, command: str, timeout: int = 120, limit: int = MAX_OUT):
        """Returns (exit_code, output). exit_code -1 means timeout."""
        try:
            r = subprocess.run(
                ["docker", "exec", "-w", "/app", self.container, "sh", "-c", command],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return -1, f"command timed out after {timeout}s"
        out = (r.stdout + r.stderr).strip()
        if len(out) > limit:
            out = "...[truncated]\n" + out[-limit:]   # keep the tail, errors are at the end
        return r.returncode, out

    def run_command(self, command: str) -> str:
        c = command.lower()
        if any(s in c for s in ("npm run dev", "npm start", "vite --", "npx vite", "npm run preview")):
            return ("ERROR: the dev server is already running with hot reload. "
                    "Do not start servers. Use `npm run build` to check your work.")
        code, out = self.exec(command)
        if code == -1:
            return "ERROR: command timed out after 120s"
        return f"exit code {code}\n{out}"

    def port(self) -> int:
        r = subprocess.run(["docker", "port", self.container, "5173/tcp"],
                           capture_output=True, text=True)
        lines = r.stdout.strip().splitlines()
        if not lines:
            raise RuntimeError(f"container {self.container} is not running")
        return int(lines[0].rsplit(":", 1)[1])
    
    def search_files(self, pattern: str) -> str:
        cmd = f"grep -rnF --include='*.ts' --include='*.tsx' --include='*.css' --include='*.html' -- {shlex.quote(pattern)} src index.html | head -30"
        code, out = self.exec(cmd, timeout=30)
        return out or "no matches"
    def replace_in_file(self, path: str, old: str, new: str) -> str:
        p = self._safe(path)
        if not p.is_file():
            return f"ERROR: {path} does not exist"
        if not old:
            return "ERROR: 'old' must not be empty"
        text = p.read_text()
        n = text.count(old)
        if n == 0:
            return "ERROR: 'old' text not found. It must match the file exactly, including whitespace. Re-read the file."
        if n > 1:
            return f"ERROR: 'old' text appears {n} times. Include more surrounding lines so it is unique."
        p.write_text(text.replace(old, new, 1))
        return f"replaced 1 occurrence in {path}"


def dispatch(sb: Sandbox, name: str, args: dict) -> str:
    try:
        if name == "list_files":
            return sb.list_files(args.get("path", "."))
        if name == "read_file":
            return sb.read_file(args["path"])
        if name == "write_file":
            return sb.write_file(args["path"], args["content"])
        if name == "search_files":
            return sb.search_files(args["pattern"])
        if name == "replace_in_file":
            return sb.replace_in_file(args["path"], args["old"], args["new"])
        if name == "run_command":
            return sb.run_command(args["command"])
        return f"ERROR: unknown tool {name}"
    except KeyError as e:
        return f"ERROR: missing argument {e}"
    except Exception as e:
        return f"ERROR: {e}"


def _tool(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}

S = {"type": "string"}
TOOLS = [
    _tool("list_files", "List files in the project (optionally under a subfolder).", {"path": S}, []),
    _tool("read_file", "Read a text file from the project.", {"path": S}, ["path"]),
    _tool("write_file", "Create or overwrite a file with the COMPLETE new content.",
          {"path": S, "content": S}, ["path", "content"]),
    _tool("run_command", "Run a shell command inside the project container (cwd /app).",
          {"command": S}, ["command"]),
    _tool("finish", "Call when the task is done and the build passes.", {"summary": S}, ["summary"]),
    _tool("search_files", "Search all source files for a text string (e.g. an error message or identifier). Returns file:line matches.", {"pattern": S}, ["pattern"]),
    _tool("replace_in_file",
          "Replace one exact snippet of an existing file. Prefer this over write_file for small changes.",
          {"path": S, "old": S, "new": S}, ["path", "old", "new"]),
]