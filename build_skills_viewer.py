#!/usr/bin/env python3
"""Collect every agent skill (a folder with a SKILL.md) you have and build a self-contained HTML viewer.

    python3 build_skills_viewer.py              # scans the default agent folders (or sources.json), writes skills.html
    python3 build_skills_viewer.py --open       # ...and opens it in your browser
    python3 build_skills_viewer.py --watch      # rebuild whenever a SKILL.md changes or a skill is added
    python3 build_skills_viewer.py --no-config --root ~/GitHub/some-repo   # ad-hoc scan

Standard library only (Python 3.9+). Token counts use tiktoken's o200k_base if tiktoken is installed
(a proxy: Claude's tokenizer differs), otherwise ~4 characters per token. PyYAML is used if present,
otherwise a small built-in frontmatter parser.
"""
import argparse
import datetime
import fnmatch
import glob
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", ".mypy_cache", ".tox",
             "dist", "build", ".ipynb_checkpoints"}
HIDDEN_OK = {".claude", ".agents", ".codex", ".opencode", ".cursor", ".claude-plugin", ".github"}
TEXT_EXT = {".md", ".markdown", ".txt", ".rst", ".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts",
            ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".j2", ".jinja", ".html", ".css", ".csv", ".tsv",
            ".r", ".jl", ".lua", ".c", ".h", ".cpp", ".hpp", ".f90", ".sql", ".xml", ".tex", ".sbatch", ".pbs",
            ".tmpl", ".template", ".env", ".lock", ""}
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SPEC_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
KIND_ORDER = {"installed": 0, "project": 1, "catalog": 2}
MAX_BODY_CHARS = 200_000

# Used when there is no sources.json: the standard per-user skill folders of common agents, plus project
# skills in repos under ~/GitHub, ~/src, ~/code and ~/projects. Copy sources.example.json to sources.json
# to add catalogs (cloned skill repos) or change any of this.
DEFAULT_CONFIG = {
    "sources": [
        {"label": "Claude Code (user)", "kind": "installed", "agent": "Claude Code", "path": "~/.claude/skills"},
        {"label": "Claude Code plugin", "kind": "installed", "agent": "Claude Code", "path": "~/.claude/plugins",
         "type": "claude-plugins"},
        {"label": "~/.agents", "kind": "installed", "agent": "Codex / OpenCode / Cursor", "path": "~/.agents/skills"},
        {"label": "Codex (user)", "kind": "installed", "agent": "Codex / OpenCode / Cursor", "path": "~/.codex/skills"},
        {"label": "OpenCode (user)", "kind": "installed", "agent": "Codex / OpenCode / Cursor",
         "path": "~/.config/opencode/skills"},
        {"label": "Pi (user)", "kind": "installed", "agent": "Pi", "path": "~/.pi/agent/skills"},
        {"label": "Copilot (user)", "kind": "installed", "agent": "Copilot", "path": "~/.copilot/skills"},
    ] + [{"label": "Project", "kind": "project", "path": f"~/{d}/{depth}{agent_dir}/skills"}
         for d in ("GitHub", "src", "code", "projects") for depth in ("*/", "*/*/")
         for agent_dir in (".claude", ".agents")],
    "exclude": ["*/node_modules*", "*/tests/fixtures*"],
    # Claude Code reportedly lists skill descriptions up to ~1% of the model's context window (unofficial; not in
    # Anthropic's docs). Shown as a bar with a context-size toggle. Other agents: totals only, unless you set a budget.
    "budgets": {"Claude Code": {"fraction": 0.01, "contexts": [200000, 1000000],
                                "note": "Unofficial estimate: ~1% of the model's context window."}},
}


# ----------------------------------------------------------------------------- tokens
class Tokenizer:
    def __init__(self, choice="auto"):
        self.enc = None
        self.label = "≈ characters ÷ 4"
        if choice in ("auto", "tiktoken"):
            try:
                import tiktoken  # noqa: F401
                self.enc = tiktoken.get_encoding("o200k_base")
                self.label = "tiktoken o200k_base (a proxy; Claude's tokenizer differs)"
            except Exception as exc:  # not installed, or no cached BPE file and no network
                if choice == "tiktoken":
                    sys.exit(f"tiktoken unavailable: {exc}")

    def count(self, text):
        if not text:
            return 0
        if self.enc is not None:
            return len(self.enc.encode(text, disallowed_special=()))
        return max(1, round(len(text) / 4))


# ----------------------------------------------------------------------------- frontmatter
def split_frontmatter(text):
    if text.startswith("﻿"):
        text = text[1:]
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return re.sub(r"\s+#.*$", "", v)


def _indent(s):
    return len(s) - len(s.lstrip(" "))


def mini_yaml(src):
    """Enough YAML for skill frontmatter: scalars, quoted strings, folded/literal blocks, flat lists and maps."""
    lines = src.splitlines()
    out, i = {}, 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#") or _indent(line) > 0:
            i += 1
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$", line)
        i += 1
        if not m:
            continue
        key, rest = m.group(1), m.group(2)
        block = []
        while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) > 0):
            block.append(lines[i])
            i += 1
        while block and not block[-1].strip():
            block.pop()
        if rest[:1] in (">", "|"):
            base = min((_indent(x) for x in block if x.strip()), default=0)
            body = [x[base:] if x.strip() else "" for x in block]
            if rest[0] == "|":
                out[key] = "\n".join(body)
            else:
                paras, cur = [], []
                for x in body:
                    if x.strip():
                        cur.append(x.strip())
                    else:
                        paras.append(" ".join(cur))
                        cur = []
                paras.append(" ".join(cur))
                out[key] = "\n".join(p for p in paras if p)
        elif rest.strip() == "":
            items = [x.strip() for x in block if x.strip() and not x.strip().startswith("#")]
            if items and all(x == "-" or x.startswith("- ") for x in items):
                out[key] = [_unquote(x[1:]) for x in items]
            elif items:
                sub = {}
                for x in items:
                    mm = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$", x)
                    if mm:
                        sub[mm.group(1)] = _unquote(mm.group(2))
                out[key] = sub
            else:
                out[key] = ""
        else:
            if block:  # plain scalar continued on indented lines
                out[key] = " ".join([rest.strip()] + [x.strip() for x in block if x.strip()])
            else:
                out[key] = _unquote(rest)
    return out


def parse_frontmatter(src):
    try:
        import yaml
    except ImportError:
        return mini_yaml(src), None
    try:
        data = yaml.safe_load(src)
        if isinstance(data, dict):
            return data, None
        return mini_yaml(src), "frontmatter is not a YAML mapping"
    except Exception as exc:
        return mini_yaml(src), "YAML parse error: " + str(exc).splitlines()[0]


def jsonable(v):
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


# ----------------------------------------------------------------------------- discovery
def expand(path):
    path = os.path.expandvars(os.path.expanduser(path))
    if any(ch in path for ch in "*?["):
        return sorted(glob.glob(path))
    return [path]


def walk_for_skills(base, excludes):
    """Every directory under base that holds a SKILL.md (follows symlinks, no loops)."""
    seen, hits = set(), []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and (not d.startswith(".") or d in HIDDEN_OK)
                             and not any(fnmatch.fnmatch(os.path.join(dirpath, d), e) for e in excludes))
        if "SKILL.md" in filenames:
            hits.append(dirpath)
    return hits


def immediate_skills(base):
    """Skills an agent would load from a skills folder: base/<name>/SKILL.md, one level deep."""
    hits, broken = [], []
    if os.path.isfile(os.path.join(base, "SKILL.md")):
        hits.append(base)
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return hits, broken
    for e in entries:
        p = os.path.join(base, e)
        if os.path.islink(p) and not os.path.exists(p):
            broken.append(p)
        elif os.path.isfile(os.path.join(p, "SKILL.md")):
            hits.append(p)
    return hits, broken


def claude_plugin_dirs(plugins_root):
    """Install paths of Claude Code plugins: from installed_plugins.json when present, else the plugin cache."""
    paths = []
    meta = os.path.join(plugins_root, "installed_plugins.json")
    if os.path.isfile(meta):
        try:
            data = json.load(open(meta))

            def grab(o):
                if isinstance(o, dict):
                    for k, v in o.items():
                        if k == "installPath" and isinstance(v, str):
                            paths.append(os.path.expanduser(v))
                        else:
                            grab(v)
                elif isinstance(o, list):
                    for v in o:
                        grab(v)
            grab(data)
        except (OSError, ValueError):
            pass
    if not paths:
        cache = os.path.join(plugins_root, "cache")
        if os.path.isdir(cache):
            paths.append(cache)
    return [p for p in dict.fromkeys(paths) if os.path.isdir(p)]


def find_plugin(skill_dir, stop):
    """Nearest .claude-plugin/plugin.json above a skill, and a marketplace.json above that."""
    plugin, market = None, None
    d = os.path.abspath(skill_dir)
    stop = os.path.abspath(stop)
    for _ in range(8):
        pj = os.path.join(d, ".claude-plugin", "plugin.json")
        mj = os.path.join(d, ".claude-plugin", "marketplace.json")
        if plugin is None and os.path.isfile(pj):
            try:
                j = json.load(open(pj))
                plugin = {"name": j.get("name"), "version": j.get("version"), "description": j.get("description")}
            except (OSError, ValueError):
                plugin = {"name": os.path.basename(d)}
        if market is None and os.path.isfile(mj):
            try:
                market = json.load(open(mj)).get("name")
            except (OSError, ValueError):
                pass
        if d == stop or os.path.dirname(d) == d:
            break
        d = os.path.dirname(d)
    if plugin and market:
        plugin["marketplace"] = market
    return plugin


_git_cache = {}


def git_info(path):
    d = os.path.realpath(path)
    top = None
    probe = d
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            top = probe
            break
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if top is None:
        return None
    if top in _git_cache:
        return _git_cache[top]

    def run(*args):
        try:
            r = subprocess.run(["git", "-C", top] + list(args), capture_output=True, text=True, timeout=5)
            return r.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    info = {"root": top, "remote": run("config", "--get", "remote.origin.url"),
            "branch": run("rev-parse", "--abbrev-ref", "HEAD"), "commit": run("rev-parse", "--short", "HEAD")}
    _git_cache[top] = info
    return info


# ----------------------------------------------------------------------------- one skill
def read_text(path, limit=2_000_000):
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, "rb") as f:
            raw = f.read()
        if b"\x00" in raw[:4096]:
            return None
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def skill_files(skill_dir, tok):
    """Resource files of a skill (everything but SKILL.md), skipping nested skills."""
    files = []
    for dirpath, dirnames, filenames in os.walk(skill_dir):
        if dirpath != skill_dir and "SKILL.md" in filenames:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            if dirpath == skill_dir and fn == "SKILL.md":
                continue
            if fn.startswith(".") or fn.endswith((".pyc", ".DS_Store")):
                continue
            p = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            ext = os.path.splitext(fn)[1].lower()
            tokens = None
            if ext in TEXT_EXT or fn in ("LICENSE", "NOTICE", "Makefile", "Dockerfile"):
                txt = read_text(p)
                if txt is not None:
                    tokens = tok.count(txt)
            files.append({"path": os.path.relpath(p, skill_dir), "bytes": size, "tokens": tokens})
    return files


def check(rec, fm, body, fm_error):
    w = []
    add = lambda level, msg: w.append({"level": level, "msg": msg})  # noqa: E731
    if fm is None:
        add("error", "No YAML frontmatter: agents will not see this as a skill.")
        return w
    if fm_error:
        add("error", fm_error)
    name, desc = fm.get("name"), fm.get("description")
    if not name:
        add("error", "Missing required field: name.")
    else:
        name = str(name)
        if len(name) > 64:
            add("error", f"name is {len(name)} characters (max 64).")
        if not NAME_RE.match(name):
            add("error", "name must be lowercase letters, digits and single hyphens, not starting or ending "
                         "with a hyphen.")
        if name != rec["dir_name"]:
            add("warn", f"name '{name}' does not match its folder '{rec['dir_name']}'.")
    if not desc:
        add("error", "Missing required field: description (the agent decides when to use a skill from it).")
    elif len(str(desc)) > 1024:
        add("error", f"description is {len(str(desc))} characters (max 1024).")
    comp = fm.get("compatibility")
    if comp and len(str(comp)) > 500:
        add("warn", f"compatibility is {len(str(comp))} characters (max 500).")
    if rec["lines"] > 500:
        add("warn", f"SKILL.md body is {rec['lines']} lines (spec recommends under 500; move detail to references/).")
    if rec["tokens"]["body"] > 5000:
        add("warn", f"SKILL.md body is ~{rec['tokens']['body']:,} tokens (spec recommends under 5,000).")
    tools = str(fm.get("allowed-tools") or "")
    if re.search(r"\bBash\((\*|\*:\*)\)|\bBash\b(?!\()", tools):
        add("info", "allowed-tools pre-approves any shell command (Bash(*)). Read the skill before trusting it.")
    extra = sorted(k for k in fm if k not in SPEC_KEYS)
    if extra:
        add("info", "Keys outside the Agent Skills spec: " + ", ".join(extra) + " (some agents use them).")
    return w


def load_skill(skill_dir, tok):
    skill_md = os.path.join(skill_dir, "SKILL.md")
    text = read_text(skill_md, limit=5_000_000) or ""
    fm_src, body = split_frontmatter(text)
    fm, fm_error = (None, None)
    if fm_src is not None:
        fm, fm_error = parse_frontmatter(fm_src)
    fmd = fm or {}
    real = os.path.realpath(skill_dir)
    name = str(fmd.get("name") or os.path.basename(real))
    desc = fmd.get("description") or ""
    if not isinstance(desc, str):
        desc = json.dumps(jsonable(desc))
    desc = desc.strip()
    files = skill_files(real, tok)
    meta_text = f"{name}: {desc}"
    rec = {
        "id": hashlib.sha1(real.encode()).hexdigest()[:12],
        "name": name,
        "dir_name": os.path.basename(real),
        "description": desc,
        "frontmatter": jsonable({k: v for k, v in fmd.items() if k not in ("name", "description")}),
        "body": body[:MAX_BODY_CHARS],
        "body_truncated": len(body) > MAX_BODY_CHARS,
        "outline": [(len(m.group(1)), m.group(2).strip()) for m in re.finditer(r"^(#{1,3})\s+(.+)$", body, re.M)],
        "lines": body.count("\n") + (1 if body and not body.endswith("\n") else 0),
        "tokens": {"meta": tok.count(meta_text), "body": tok.count(body),
                   "resources": sum(f["tokens"] or 0 for f in files)},
        "chars_meta": len(meta_text),
        "files": files,
        "real": real,
        "git": git_info(real),
        "modified": None,
        "locations": [],
    }
    rec["tokens"]["total"] = rec["tokens"]["meta"] + rec["tokens"]["body"] + rec["tokens"]["resources"]
    mt = [os.path.getmtime(skill_md)] if os.path.exists(skill_md) else []
    for f in files:
        try:
            mt.append(os.path.getmtime(os.path.join(real, f["path"])))
        except OSError:
            pass
    if mt:
        rec["modified"] = datetime.datetime.fromtimestamp(max(mt)).isoformat(timespec="minutes")
    rec["warnings"] = check(rec, fm, body, fm_error)
    return rec


# ----------------------------------------------------------------------------- display paths
class Paths:
    def __init__(self, maps, home):
        self.maps = sorted(maps, key=lambda m: -len(m[0]))
        self.home = home.rstrip("/")

    def abs(self, p):
        for old, new in self.maps:
            if p == old or p.startswith(old.rstrip("/") + "/"):
                return new.rstrip("/") + p[len(old.rstrip("/")):]
        return p

    def show(self, p):
        p = self.abs(p)
        if self.home and (p == self.home or p.startswith(self.home + "/")):
            return "~" + p[len(self.home):]
        return p


# ----------------------------------------------------------------------------- build
def load_config(path):
    if not path or not os.path.isfile(path):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(path) as f:
        cfg = json.load(f)
    cfg.setdefault("sources", [])
    cfg.setdefault("budgets", {})
    return cfg


def collect(cfg, extra_roots, tok, paths):
    skills = {}
    notes = []
    excludes = [os.path.expanduser(e) for e in cfg.get("exclude", [])]
    sources = list(cfg["sources"])
    for r in extra_roots:
        sources.append({"label": os.path.basename(os.path.abspath(os.path.expanduser(r))) or r, "kind": "catalog",
                        "path": r, "recursive": True})
    stats = []
    for src in sources:
        label, kind = src.get("label", src["path"]), src.get("kind", "catalog")
        agent, recursive = src.get("agent"), bool(src.get("recursive", kind == "catalog"))
        st = {"label": label, "kind": kind, "path": paths.show(os.path.expanduser(src["path"])), "exists": False,
              "found": 0}
        stats.append(st)
        for base in expand(src["path"]):
            if not os.path.isdir(base):
                continue
            st["exists"] = True
            if src.get("type") == "claude-plugins":
                bases, rec_flag = claude_plugin_dirs(base), True
            else:
                bases, rec_flag = [base], recursive
            for b in bases:
                if rec_flag:
                    found, broken = walk_for_skills(b, excludes), []
                else:
                    found, broken = immediate_skills(b)
                for p in broken:
                    notes.append(f"Broken symlink in {paths.show(b)}: {os.path.basename(p)}")
                st["found"] += len(found)
                for d in found:
                    real = os.path.realpath(d)
                    if real not in skills:
                        skills[real] = load_skill(real, tok)
                    rec = skills[real]
                    plugin = find_plugin(d, b) if (rec_flag or src.get("type") == "claude-plugins") else None
                    loc_label = label
                    if src.get("type") == "claude-plugins" and plugin and plugin.get("name"):
                        loc_label = f"{label}: {plugin['name']}"
                    if label.endswith("(project)") or kind == "project":
                        # name the project after the repo that holds .claude/skills
                        parts = os.path.abspath(d).split(os.sep)
                        if ".claude" in parts:
                            loc_label = f"Project: {parts[parts.index('.claude') - 1]}"
                    loc = {"source": loc_label, "kind": kind, "agent": agent, "path": paths.show(d),
                           "abs": paths.abs(d), "via_symlink": os.path.realpath(d) != os.path.abspath(d)}
                    if plugin:
                        loc["plugin"] = plugin
                    rel = os.path.relpath(os.path.dirname(os.path.abspath(d)), b)
                    cat = "" if rel.startswith("..") else \
                        "/".join(p for p in rel.split(os.sep) if p not in (".", "", "skills", "plugins"))
                    if rec_flag and cat:
                        loc["category"] = cat
                    if not any(x["path"] == loc["path"] for x in rec["locations"]):
                        rec["locations"].append(loc)
    out = []
    for rec in skills.values():
        rec["locations"].sort(key=lambda x: (KIND_ORDER.get(x["kind"], 9), x["source"]))
        rec["kinds"] = sorted({x["kind"] for x in rec["locations"]}, key=lambda k: KIND_ORDER.get(k, 9))
        rec["skill_md"] = paths.show(os.path.join(rec["real"], "SKILL.md"))
        rec["real_abs"] = paths.abs(rec["real"])
        rec["real"] = paths.show(rec["real"])
        if rec["git"]:
            rec["git"]["root"] = paths.show(rec["git"]["root"])
        out.append(rec)
    # name clashes among skills an agent loads together
    by_scope = {}
    for rec in out:
        for loc in rec["locations"]:
            if loc["kind"] in ("installed", "project"):
                by_scope.setdefault((loc["source"], rec["name"]), set()).add(rec["id"])
    for rec in out:
        clash = [s for (s, nm), ids in by_scope.items() if nm == rec["name"] and len(ids) > 1]
        if clash:
            rec["warnings"].append({"level": "warn", "msg": "Another skill with the same name is installed in "
                                    + ", ".join(sorted(set(clash))) + "; one will shadow the other."})
    # a project skill and a user-level skill with the same name are both visible inside that project
    for rec in out:
        own = {(loc["source"], loc["kind"]) for loc in rec["locations"]}
        for other in out:
            if other is rec or other["name"] != rec["name"]:
                continue
            for loc in other["locations"]:
                for src, kind in own:
                    if {kind, loc["kind"]} == {"project", "installed"}:
                        proj = src if kind == "project" else loc["source"]
                        inst = loc["source"] if kind == "project" else src
                        msg = (f"Inside {proj.replace('Project: ', '')}, the agent sees two different "
                               f"'{rec['name']}' skills: the project's own and the one installed in {inst}. "
                               "Rename one or uninstall the user-level copy.")
                        if not any(w["msg"] == msg for w in rec["warnings"]):
                            rec["warnings"].append({"level": "warn", "msg": msg})
    out.sort(key=lambda r: (KIND_ORDER.get(r["kinds"][0], 9) if r["kinds"] else 9, r["name"]))
    # always-loaded budget per installed source
    groups = {}
    for rec in out:
        for loc in rec["locations"]:
            if loc["kind"] == "installed":
                key = loc["agent"] or loc["source"]
                g = groups.setdefault(key, {"agent": key, "sources": set(), "skills": 0, "chars": 0, "tokens": 0})
                g["sources"].add(loc["source"].split(":")[0])
                g["skills"] += 1
                g["chars"] += rec["chars_meta"]
                g["tokens"] += rec["tokens"]["meta"]
    budgets = cfg.get("budgets", {})
    groups_out = []
    for key, g in sorted(groups.items()):
        g["sources"] = sorted(g["sources"])
        g["budget"] = budgets.get(key)
        groups_out.append(g)
    return out, groups_out, notes, stats


def render(skills, groups, notes, tok, template_path, out_path, sources_desc):
    data = {
        "generated": datetime.datetime.now().isoformat(timespec="minutes"),
        "host": platform.node(),
        "tokenizer": tok.label,
        "skills": skills,
        "groups": groups,
        "notes": notes,
        "sources": sources_desc,
        "budget_note": "Budget = an estimate of the characters an agent will show from skill names and "
                       "descriptions before it starts truncating; set it in sources.json.",
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    with open(template_path, encoding="utf-8") as f:
        tpl = f.read()
    html = tpl.replace("/*__SKILLS_DATA__*/", blob)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, out_path)


def fingerprint(cfg, extra_roots):
    """Cheap signature of every SKILL.md under the configured sources, for --watch."""
    sig = []
    for src in list(cfg["sources"]) + [{"path": r} for r in extra_roots]:
        for base in expand(src["path"]):
            for p in glob.glob(os.path.join(base, "**", "SKILL.md"), recursive=True):
                try:
                    sig.append((p, os.path.getmtime(p)))
                except OSError:
                    pass
            try:
                sig.append((base, os.path.getmtime(base)))
            except OSError:
                pass
    return hashlib.sha1(repr(sorted(sig)).encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(HERE, "sources.json"),
                    help="sources file (default: %(default)s; built-in defaults if it does not exist)")
    ap.add_argument("--no-config", action="store_true", help="ignore sources.json and the defaults; use only --root")
    ap.add_argument("--root", action="append", default=[], help="extra folder to scan recursively (repeatable)")
    ap.add_argument("-o", "--out", default=os.path.join(HERE, "skills.html"), help="output HTML (default: %(default)s)")
    ap.add_argument("--template", default=os.path.join(HERE, "viewer_template.html"))
    ap.add_argument("--tokenizer", choices=["auto", "tiktoken", "chars"], default="auto")
    ap.add_argument("--path-map", action="append", default=[], metavar="FROM=TO",
                    help="rewrite displayed paths, e.g. when scanning a mounted copy (repeatable)")
    ap.add_argument("--home", default=os.path.expanduser("~"), help="home folder shown as ~ (default: yours)")
    ap.add_argument("--open", action="store_true", help="open the page when done")
    ap.add_argument("--watch", nargs="?", const=5.0, type=float, metavar="SECONDS",
                    help="rebuild when skills change (poll every SECONDS, default 5)")
    args = ap.parse_args()

    cfg = {"sources": [], "budgets": {}} if args.no_config else load_config(args.config)
    if not cfg["sources"] and not args.root:
        sys.exit("Nothing to scan: add sources to sources.json or pass --root.")
    maps = [tuple(m.split("=", 1)) for m in args.path_map if "=" in m]
    paths = Paths([(os.path.expanduser(a), b) for a, b in maps], args.home)
    tok = Tokenizer(args.tokenizer)
    def build():
        t0 = time.time()
        skills, groups, notes, stats = collect(cfg, args.root, tok, paths)
        render(skills, groups, notes, tok, args.template, args.out, stats)
        n_issue = sum(1 for s in skills if any(w["level"] in ("error", "warn") for w in s["warnings"]))
        print(f"{len(skills)} skills ({sum(1 for s in skills if 'installed' in s['kinds'])} installed, "
              f"{n_issue} with issues) -> {args.out}  [{time.time() - t0:.1f}s, tokens: {tok.label}]")
        for nt in notes:
            print("  note:", nt)

    build()
    if args.open:
        webbrowser.open("file://" + os.path.abspath(args.out))
    if args.watch:
        last = fingerprint(cfg, args.root)
        print(f"watching for changes every {args.watch:g}s (Ctrl-C to stop)")
        try:
            while True:
                time.sleep(args.watch)
                now = fingerprint(cfg, args.root)
                if now != last:
                    last = now
                    build()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
