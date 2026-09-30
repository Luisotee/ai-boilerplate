#!/usr/bin/env python3
"""Keep several checkouts of this project from colliding on one host.

Everything host-global is checked against what Docker already knows about the
OTHER compose projects on the machine: published ports, container / network /
image names, and the Compose project name (which owns the volumes).

Standard library only and no imports from the repo, so a fork can copy this one
file. `setup.sh` is a thin caller; run it directly for the same commands:

    python3 scripts/instance_check.py check          # read-only report
    python3 scripts/instance_check.py fix [--yes]    # move clashing ports

Exit codes: 0 clean; 1 collisions found (the check may also be incomplete, see
its notes); 2 check incomplete and nothing found, or `fix` refused because
Docker state is unavailable; 64 usage error.
"""

import argparse
import errno
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections import namedtuple

# VAR, default. The value is the Docker host-published port; container ports are
# pinned in docker-compose.yml. Order is the order ports are assigned in.
PORT_SPECS = [
    ("POSTGRES_PORT", 5432),
    ("REDIS_PORT", 6379),
    ("ADMINER_PORT", 8080),
    ("AI_API_PORT", 8000),
    ("WHATSAPP_API_PORT", 3001),
    ("WHATSAPP_CLOUD_PORT", 3002),
    ("TELEGRAM_PORT", 3003),
    ("WHISPER_PORT", 8771),
]
# Local-dev callback URLs that follow a client port.
CLIENT_URLS = {
    "WHATSAPP_API_PORT": "WHATSAPP_CLIENT_URL",
    "WHATSAPP_CLOUD_PORT": "WHATSAPP_CLOUD_CLIENT_URL",
    "TELEGRAM_PORT": "TELEGRAM_CLIENT_URL",
}
DEFAULT_SERVICE_NAME = "aiagent"
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
PORT_SPAN = 100

EXIT_OK = 0
EXIT_CLASH = 1
EXIT_INCOMPLETE = 2
EXIT_USAGE = 64

L_PROJECT = "com.docker.compose.project"
L_WORKDIR = "com.docker.compose.project.working_dir"
L_FILES = "com.docker.compose.project.config_files"
L_SERVICE = "com.docker.compose.service"

# source: "bound" (a container exists with this port), "declared" (a compose
# file publishes it, container not created), "listening" (a non-Docker socket).
Claim = namedtuple("Claim", "port owner service source")
Container = namedtuple("Container", "name project working_dir config_files service ports")
# name + compose project label ("" when not created by compose)
Resource = namedtuple("Resource", "name project")

GUARD_OK = "OK"
GUARD_OFFER = "OFFER"
GUARD_REFUSE = "REFUSE"


# ── .env files (Compose's dotenv rules) ─────────────────────────────────────

_ENV_LINE = re.compile(r"^\s*(export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _unquote(raw):
    """Value of a dotenv assignment: quoted up to the matching quote, otherwise
    up to an inline ` #` comment, trimmed."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        return raw[1:end] if end != -1 else raw[1:]
    comment = re.search(r"\s#", raw)
    return (raw[: comment.start()] if comment else raw).strip()


def _body(line):
    return line.rstrip("\r\n")


def parse_env(text):
    """Active assignments of a .env file. As in Compose, the last one wins."""
    env = {}
    for line in text.splitlines():
        match = _ENV_LINE.match(line)
        if match:
            env[match.group(2)] = _unquote(match.group(3))
    return env


def set_env(text, key, value):
    """Rewrite every assignment of KEY in place, or append one.

    An `export ` prefix, the value's quote style and each line's own ending
    (LF or CRLF) are kept; no other line is touched.
    """
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    found = False
    for index, line in enumerate(lines):
        match = _ENV_LINE.match(_body(line))
        if not match or match.group(2) != key:
            continue
        found = True
        old = match.group(3).strip()
        quote = old[0] if old[:1] in ("'", '"') else ""
        ending = line[len(_body(line)) :]
        lines[index] = f"{match.group(1) or ''}{key}={quote}{value}{quote}{ending}"
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += newline
        lines.append(f"{key}={value}{newline}")
    return "".join(lines)


def missing_keys(template_text, env_text):
    """Template keys absent from .env, in template order."""
    env = parse_env(env_text)
    return [key for key in parse_env(template_text) if key not in env]


# ── Pure helpers ────────────────────────────────────────────────────────────


def normalize_project_name(basename):
    """Compose's rule for a directory-derived project name."""
    name = re.sub(r"[^a-z0-9_-]", "", basename.lower())
    return name.lstrip("_-")


def effective_project(env, shell_env, dirname):
    """The Compose project name this checkout resolves to (no top-level `name:`)."""
    explicit = shell_env.get("COMPOSE_PROJECT_NAME") or env.get("COMPOSE_PROJECT_NAME")
    return explicit or normalize_project_name(os.path.basename(dirname.rstrip("/")))


def _to_port(value):
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def _expand_ports(value):
    """ "8000" -> [8000]; "8000-8002" -> [8000, 8001, 8002]; anything else -> []."""
    if value is None:
        return []
    text = str(value).strip()
    if "-" in text:
        low, _, high = text.partition("-")
        low, high = _to_port(low), _to_port(high)
        if low is None or high is None or high < low or high - low > 1000:
            return []
        return list(range(low, high + 1))
    port = _to_port(text)
    return [port] if port is not None else []


def parse_containers(inspected):
    """`docker inspect` output (list of objects) -> [Container] with TCP host ports."""
    containers = []
    for item in inspected or []:
        labels = (item.get("Config") or {}).get("Labels") or {}
        bindings = (item.get("HostConfig") or {}).get("PortBindings") or {}
        ports = []
        for key, hosts in bindings.items():
            if key.endswith("/udp"):
                continue
            for host in hosts or []:
                ports.extend(_expand_ports((host or {}).get("HostPort") or None))
        containers.append(
            Container(
                name=(item.get("Name") or "").lstrip("/"),
                project=labels.get(L_PROJECT, ""),
                working_dir=labels.get(L_WORKDIR, ""),
                config_files=[f for f in labels.get(L_FILES, "").split(",") if f],
                service=labels.get(L_SERVICE, ""),
                ports=sorted(set(ports)),
            )
        )
    return containers


def ports_from_config(config, fallback_owner=""):
    """`docker compose config --format json` -> [Claim] for every published TCP port."""
    config = config or {}
    owner = config.get("name") or fallback_owner
    claims = []
    for service, spec in (config.get("services") or {}).items():
        for port in (spec or {}).get("ports") or []:
            if not isinstance(port, dict) or port.get("protocol", "tcp") != "tcp":
                continue
            for number in _expand_ports(port.get("published")):
                claims.append(Claim(number, owner, service, "declared"))
    return claims


def same_dir(path_a, path_b):
    if not path_a or not path_b:
        return False
    return os.path.realpath(path_a) == os.path.realpath(path_b)


def is_moved(container, project, exists=os.path.isdir):
    """A container of our project whose recorded directory is gone: this
    checkout was moved or renamed after the stack was created."""
    return bool(
        project
        and container.project == project
        and container.working_dir
        and not exists(container.working_dir)
    )


def partition(containers, own_dir, project="", exists=os.path.isdir):
    """Split into (ours, foreign) by the compose working directory. A container
    started with plain `docker run` has no label and counts as foreign."""
    own, foreign = [], []
    for container in containers:
        mine = same_dir(container.working_dir, own_dir) or is_moved(container, project, exists)
        (own if mine else foreign).append(container)
    return own, foreign


def claims_from_containers(containers):
    return [
        Claim(port, c.project or c.name, c.service or c.name, "bound")
        for c in containers
        for port in c.ports
    ]


def allocate(specs, current, is_free, span=PORT_SPAN):
    """Pick a host port per variable.

    A variable keeps its current port while that port is free; otherwise it gets
    the first free port at or above its default. No port is handed out twice.
    Returns (assignments, errors): `errors` lists the variables for which nothing
    was free within `span`; they keep their current (or default) value.
    """
    assignments, errors, chosen = {}, [], set()
    for var, _default in specs:
        port = _to_port(current.get(var))
        if port is not None and port not in chosen and is_free(port):
            assignments[var] = port
            chosen.add(port)
    for var, default in specs:
        if var in assignments:
            continue
        for port in range(default, default + span + 1):
            if port not in chosen and is_free(port):
                assignments[var] = port
                chosen.add(port)
                break
        else:
            errors.append(var)
            assignments[var] = _to_port(current.get(var)) or default
    return assignments, errors


def container_suffixes(compose_text):
    """Service suffixes of `container_name: ${SERVICE_NAME…}-<suffix>` lines."""
    return re.findall(
        r"container_name:\s*['\"]?\$\{SERVICE_NAME[^}]*\}-([A-Za-z0-9_.-]+)", compose_text
    )


def name_conflicts(service_name, suffixes, containers, networks, image_project, own_dir, project):
    """Things named after SERVICE_NAME that another compose project already owns.

    Returns (errors, warnings). The image is only a warning: its label records
    the last project that built the tag, which may be long gone.
    """
    errors, warnings = [], []
    wanted = {f"{service_name}-{suffix}" for suffix in suffixes}
    _own, foreign = partition(containers, own_dir, project)
    for container in foreign:
        if wanted:
            clash = container.name in wanted
        else:
            clash = container.name.startswith(service_name + "-")
        if clash:
            owner = container.project or "a non-compose container"
            where = f" ({container.working_dir})" if container.working_dir else ""
            errors.append(f"container '{container.name}' already exists, owned by {owner}{where}")
    network_name = f"{service_name}-network"
    for network in networks:
        if network.name == network_name and network.project != project:
            owner = network.project or "something outside compose"
            errors.append(f"network '{network_name}' already exists, owned by {owner}")
    if image_project and image_project != project:
        warnings.append(
            f"image '{service_name}-api:latest' was last built by project '{image_project}'; "
            "building here replaces that tag"
        )
    return errors, warnings


def project_guard(project, explicit, containers, volumes, networks, own_dir, fresh, candidate):
    """Is the Compose project name (= volume owner) shared with another checkout?

    OK      nothing else uses it, COMPOSE_PROJECT_NAME is set explicitly, or only
            volumes/networks are left and a .env already exists (a stopped stack
            of this checkout; the reason then carries a note).
    OFFER   safe to set COMPOSE_PROJECT_NAME=<candidate>: this is a first setup
            (no .env before, no containers of ours) and the candidate is unused.
    REFUSE  another checkout's containers use it, and changing the name here
            could orphan existing volumes.
    Returns (status, reason).
    """
    if explicit:
        return GUARD_OK, ""
    own, foreign = partition(containers, own_dir, project)
    foreign_same = [c for c in foreign if c.project == project]
    leftovers = [r.name for r in list(volumes) + list(networks) if r.project == project]
    if foreign_same:
        dirs = sorted({c.working_dir for c in foreign_same if c.working_dir})
        where = ", ".join(dirs) or "unknown directory"
        reason = f"another checkout uses the Compose project name '{project}': {where}"
    elif leftovers and not own:
        listed = ", ".join(sorted(leftovers)[:4])
        if not fresh:
            return GUARD_OK, (
                f"volumes/networks of project '{project}' exist without containers ({listed}); "
                "assumed to be this checkout's stopped stack"
            )
        reason = f"volumes/networks of a Compose project named '{project}' already exist ({listed})"
    else:
        return GUARD_OK, ""
    used = {c.project for c in containers} | {r.project for r in list(volumes) + list(networks)}
    if fresh and not own and candidate and candidate != project and candidate not in used:
        return GUARD_OFFER, reason
    return GUARD_REFUSE, reason


def sync_derived(env_text, old_ports, new_ports):
    """Follow a port change in the local-dev URLs that embed it.

    Only a URL that still has the exact expected shape is rewritten; anything
    hand-edited is left alone and reported. Returns (text, warnings).
    """
    env = parse_env(env_text)
    warnings = []
    old, new = old_ports.get("POSTGRES_PORT"), new_ports.get("POSTGRES_PORT")
    if old and new and old != new and "DATABASE_URL" in env:
        pattern = re.compile(rf"@(localhost|127\.0\.0\.1):{old}/")
        if pattern.search(env["DATABASE_URL"]):
            url = pattern.sub(rf"@\g<1>:{new}/", env["DATABASE_URL"])
            env_text = set_env(env_text, "DATABASE_URL", url)
        else:
            warnings.append(f"DATABASE_URL does not point at localhost:{old}; update it by hand")
    for port_var, url_var in CLIENT_URLS.items():
        old, new = old_ports.get(port_var), new_ports.get(port_var)
        if not old or not new or old == new:
            continue
        if url_var not in env or env[url_var] == f"http://localhost:{old}":
            env_text = set_env(env_text, url_var, f"http://localhost:{new}")
        else:
            warnings.append(f"{url_var} is customised; point it at port {new} by hand")
    return env_text, warnings


def port_clashes(ports, claims):
    """{var: [Claim, …]} for every configured port someone else claims."""
    by_port = {}
    for claim in claims:
        by_port.setdefault(claim.port, []).append(claim)
    return {var: by_port[port] for var, port in ports.items() if port in by_port}


def describe_claim(claim):
    if claim.source == "listening":
        return "in use by a process on this host"
    state = "container exists" if claim.source == "bound" else "stack not created"
    return f"{claim.owner} ({claim.service}, {state})"


# ── Docker / host I/O ───────────────────────────────────────────────────────


class DockerUnavailable(Exception):
    pass


def _run(args, cwd=None, env=None, timeout=15):
    try:
        done = subprocess.run(args, cwd=cwd, env=env, timeout=timeout, capture_output=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerUnavailable(type(exc).__name__) from exc
    if done.returncode != 0:
        lines = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise DockerUnavailable(lines[-1] if lines else f"exit {done.returncode}")
    return done.stdout.decode("utf-8", "replace")


def _labelled(kind):
    fmt = '{{.Name}}\t{{.Label "' + L_PROJECT + '"}}'
    out = _run(["docker", kind, "ls", "--format", fmt])
    rows = [line.split("\t") for line in out.splitlines() if line.strip()]
    return [Resource(row[0], row[1] if len(row) > 1 else "") for row in rows]


def docker_state():
    """(containers, volumes, networks). Raises DockerUnavailable."""
    if not shutil.which("docker"):
        raise DockerUnavailable("docker is not installed")
    ids = _run(["docker", "ps", "-aq"]).split()
    try:
        inspected = json.loads(_run(["docker", "inspect", *ids])) if ids else []
    except ValueError as exc:
        raise DockerUnavailable("unreadable docker inspect output") from exc
    return parse_containers(inspected), _labelled("volume"), _labelled("network")


def image_project(service_name):
    """Compose project that last built <service_name>-api:latest ('' if none)."""
    fmt = '{{index .Config.Labels "' + L_PROJECT + '"}}'
    try:
        out = _run(["docker", "image", "inspect", f"{service_name}-api:latest", "--format", fmt])
    except DockerUnavailable:
        return ""
    out = out.strip()
    return "" if out == "<no value>" else out


def sibling_config(directory, config_files):
    """Every port a neighbouring compose project publishes, profiles included.

    The rendered config contains that project's secrets: it is parsed for ports
    and dropped, never printed. Returns None when it cannot be rendered.
    """
    # Only what docker needs: an exported AI_API_PORT or COMPOSE_* in our shell
    # would otherwise override the neighbour's own .env.
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME") or k.startswith("DOCKER_")}
    base = ["docker", "compose"]
    for path in config_files:
        base += ["-f", path]
    for profile in (["--profile", "*"], []):
        try:
            out = _run([*base, *profile, "config", "--format", "json"], directory, env, 20)
            return ports_from_config(json.loads(out), os.path.basename(directory))
        except (DockerUnavailable, ValueError):
            continue
    return None


def sibling_dirs(own_dir, foreign):
    """{dir: config files} of other compose projects: those Docker has containers
    for, the neighbours of this checkout, and anything in SIBLING_DIRS."""
    found = {}
    for container in foreign:
        if container.working_dir and os.path.isdir(container.working_dir):
            files = [f for f in container.config_files if os.path.isfile(f)]
            found.setdefault(os.path.realpath(container.working_dir), files)
    extra = [d for d in os.environ.get("SIBLING_DIRS", "").split(":") if d]
    for directory in glob.glob(os.path.join(os.path.dirname(own_dir), "*")) + extra:
        if any(os.path.isfile(os.path.join(directory, name)) for name in COMPOSE_FILES):
            found.setdefault(os.path.realpath(directory), [])
    found.pop(os.path.realpath(own_dir), None)
    return found


def is_listening(port):
    """True if a TCP socket on this host already listens on the port (any address)."""
    for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            probe = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            probe.bind((address, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return True
        finally:
            probe.close()
    return False


class Host:
    """Everything known about the host. Built from data so tests can inject it;
    `Host.from_docker` gathers the real state."""

    def __init__(
        self,
        own_dir,
        project="",
        containers=(),
        volumes=(),
        networks=(),
        declared=(),
        docker_error="",
        unreadable=(),
        listening=is_listening,
    ):
        self.own_dir = os.path.realpath(own_dir)
        self.project = project
        self.containers, self.volumes, self.networks = (
            list(containers),
            list(volumes),
            list(networks),
        )
        self.docker_error = docker_error
        self.unreadable = list(unreadable)
        self.listening = listening
        own, foreign = partition(self.containers, self.own_dir, project)
        self.own_ports = {port for c in own for port in c.ports}
        # Ports presumed ours without proof (set when Docker can't tell).
        self.assumed_own = set()
        self.moved = [c.name for c in own if not same_dir(c.working_dir, self.own_dir)]
        claims = claims_from_containers(foreign)
        seen = {(c.port, c.owner) for c in claims}
        for claim in declared:
            if (claim.port, claim.owner) not in seen:
                seen.add((claim.port, claim.owner))
                claims.append(claim)
        self.claims = claims
        self.claimed = {c.port for c in claims}

    @classmethod
    def from_docker(cls, project_dir, project):
        own_dir = os.path.realpath(project_dir)
        containers, volumes, networks, error = [], [], [], ""
        try:
            containers, volumes, networks = docker_state()
        except DockerUnavailable as exc:
            error = str(exc) or "unknown error"
        _own, foreign = partition(containers, own_dir, project)
        declared, unreadable = [], []
        if shutil.which("docker"):
            for directory, files in sorted(sibling_dirs(own_dir, foreign).items()):
                claims = sibling_config(directory, files)
                if claims is None:
                    unreadable.append(directory)
                else:
                    declared.extend(claims)
        return cls(own_dir, project, containers, volumes, networks, declared, error, unreadable)

    @property
    def incomplete(self):
        return bool(self.docker_error or self.unreadable)

    def is_free(self, port):
        # Another project's claim wins even when our own container has the port
        # too: that is exactly the clash `fix` exists to move.
        if port in self.claimed:
            return False
        if port in self.own_ports or port in self.assumed_own:
            return True
        return not self.listening(port)

    def clashes(self, ports):
        """{var: [Claim]} including ports held by a non-Docker process."""
        found = port_clashes(ports, self.claims)
        for var, port in ports.items():
            if var in found or port in self.own_ports or port in self.assumed_own:
                continue
            if self.listening(port):
                found[var] = [Claim(port, "", "", "listening")]
        return found


def load_host(project_dir, project):
    """Seam for tests: the commands never build a Host any other way."""
    return Host.from_docker(project_dir, project)


# ── Commands ────────────────────────────────────────────────────────────────


def _read(path):
    # newline="" keeps CRLF line endings intact.
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_env(path, text):
    """Replace the file atomically. A symlinked .env stays a symlink: the file it
    points at is the one replaced."""
    target = os.path.realpath(path)
    tmp = target + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.replace(tmp, target)


def _backup(path):
    """Byte-for-byte copy of the current file, mode 600."""
    backup = f"{path}.bak.{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copyfile(os.path.realpath(path), backup)
    os.chmod(backup, 0o600)
    return backup


def _configured_ports(env):
    """The port each variable resolves to: the .env value, else compose's default."""
    return {var: _to_port(env.get(var)) or default for var, default in PORT_SPECS}


def _suffixes(project_dir):
    for name in COMPOSE_FILES:
        path = os.path.join(project_dir, name)
        if os.path.isfile(path):
            return container_suffixes(_read(path))
    return []


def _explicit_project(env):
    return bool(os.environ.get("COMPOSE_PROJECT_NAME") or env.get("COMPOSE_PROJECT_NAME"))


def _host_notes(host):
    notes = []
    if host.moved:
        notes.append(
            "containers recorded under a directory that no longer exists are treated as this "
            f"checkout's (moved or renamed?): {', '.join(host.moved)}"
        )
    for directory in host.unreadable:
        notes.append(
            f"could not render the compose config in {directory}; its unstarted services are not counted"
        )
    if host.docker_error:
        notes.append(
            f"Docker state unavailable ({host.docker_error}): only live sockets and "
            "neighbouring compose files were checked"
        )
    return notes


def _guard_help(project, manual=True):
    lines = [
        f"Both checkouts would share the '{project}_*' volumes (database, WhatsApp session, uploads)",
        "and `docker compose up/down` in one would replace the other's containers.",
    ]
    if not manual:
        return lines
    return [
        *lines,
        "Fix by hand: stop the stack, decide which checkout owns those volumes, and set",
        "COMPOSE_PROJECT_NAME=<unique name> in the OTHER checkout's .env (it starts with empty volumes).",
        f"If this directory was simply moved or renamed, set COMPOSE_PROJECT_NAME={project} here instead.",
    ]


def cmd_check(args):
    env_text = _read(args.env) if os.path.isfile(args.env) else ""
    env = parse_env(env_text)
    own_dir = os.path.realpath(args.project_dir)
    project = effective_project(env, os.environ, own_dir)
    host = load_host(own_dir, project)
    service_name = env.get("SERVICE_NAME") or DEFAULT_SERVICE_NAME
    ports = _configured_ports(env)

    print(f"Instance check: SERVICE_NAME '{service_name}', Compose project '{project}'")
    problems = 0

    clashes = host.clashes(ports)
    if clashes:
        print("\nPort clashes:")
        for var, _default in PORT_SPECS:
            for claim in clashes.get(var, []):
                problems += 1
                print(f"  {var}={claim.port}: {describe_claim(claim)}")

    errors, warnings = name_conflicts(
        service_name,
        _suffixes(own_dir),
        host.containers,
        host.networks,
        "" if host.docker_error else image_project(service_name),
        own_dir,
        project,
    )
    if errors:
        problems += len(errors)
        print("\nName clashes (choose another SERVICE_NAME in .env):")
        for line in errors:
            print("  " + line)

    notes = list(warnings)
    status, reason = project_guard(
        project,
        _explicit_project(env),
        host.containers,
        host.volumes,
        host.networks,
        own_dir,
        False,
        "",
    )
    if status == GUARD_OK:
        if reason:
            notes.append(reason)
    else:
        problems += 1
        print("\nShared Compose project name: " + reason)
        for line in _guard_help(project):
            print("  " + line)

    if os.path.isfile(args.template):
        template_text = _read(args.template)
        defaults = parse_env(template_text)
        # A key the template leaves empty is optional: absent means the same.
        absent = [k for k in missing_keys(template_text, env_text) if defaults[k]]
        if absent:
            notes.append(
                f".env lacks {len(absent)} key(s) from .env.example (defaults apply): "
                + " ".join(absent)
            )
    notes += _host_notes(host)
    if notes:
        print("\nNotes:")
        for line in notes:
            print("  " + line)

    if problems:
        if clashes:
            print("\nRun ./setup.sh --fix to move the clashing ports.")
        return EXIT_CLASH
    if host.incomplete:
        print("\nNo collisions found, but the check is incomplete (see notes).")
        return EXIT_INCOMPLETE
    print("\nNo collisions found.")
    return EXIT_OK


def cmd_fix(args):
    if not os.path.isfile(args.env):
        print(f"No {args.env} found. Run ./setup.sh first.")
        return EXIT_CLASH
    env_text = _read(args.env)
    env = parse_env(env_text)
    own_dir = os.path.realpath(args.project_dir)
    host = load_host(own_dir, effective_project(env, os.environ, own_dir))
    if host.docker_error:
        # Without Docker our own running stack looks like any other listener,
        # and "fixing" would move the ports away from it.
        print(f"Docker state unavailable ({host.docker_error}).")
        print("Refusing to change ports: this checkout's containers can't be told apart")
        print("from other projects' without Docker. Nothing changed.")
        return EXIT_INCOMPLETE
    current = _configured_ports(env)
    assignments, errors = allocate(PORT_SPECS, current, host.is_free)

    moved = [
        (v, current[v], assignments[v]) for v, _d in PORT_SPECS if assignments[v] != current[v]
    ]
    pinned = [var for var, _d in PORT_SPECS if var not in env]
    for line in _host_notes(host):
        print(line)
    defaults = dict(PORT_SPECS)
    for var in errors:
        print(f"No free port near {defaults[var]} for {var}: set it by hand in .env")
    if not moved and not pinned:
        print("Ports are fine; nothing to change.")
        return EXIT_CLASH if errors else EXIT_OK

    for var, old, new in moved:
        print(f"  {var}: {old} -> {new}")
    if pinned:
        print(f"  written explicitly (were implicit defaults): {' '.join(pinned)}")
    if not args.yes:
        try:
            answer = input(f"Apply to {args.env}? (y/N): ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return EXIT_CLASH if moved else EXIT_OK

    backup = _backup(args.env)
    new_text = env_text
    for var, _default in PORT_SPECS:
        if var in pinned or assignments[var] != current[var]:
            new_text = set_env(new_text, var, str(assignments[var]))
    new_text, warnings = sync_derived(new_text, current, assignments)
    _write_env(args.env, new_text)
    print(f"Updated {args.env} (backup: {backup})")
    for line in warnings:
        print("  ! " + line)
    if moved:
        print("Apply with `docker compose up -d`. Anything that targets the old ports from outside")
        print("(reverse proxy, webhook URL, FleetView base URL, firewall rules) must follow.")
    return EXIT_CLASH if errors else EXIT_OK


def cmd_assign_ports(args):
    """First-time allocation for setup.sh. Prints one line per port:
    `PORT VAR from to` (from = the carried-over value, else the default),
    `ERROR VAR default` when nothing near the default is free, and `NOTE text`."""
    env_text = _read(args.env)
    before = _configured_ports(parse_env(env_text))
    current = {}
    for pair in args.current or []:
        var, _, value = pair.partition("=")
        current[var] = value
    own_dir = os.path.realpath(args.project_dir)
    host = load_host(own_dir, effective_project(parse_env(env_text), os.environ, own_dir))
    if host.docker_error:
        # Can't see our own containers: keep carried-over ports rather than
        # bumping them away from what is probably our own stack.
        host.assumed_own = {p for p in map(_to_port, current.values()) if p}
    assignments, errors = allocate(PORT_SPECS, current, host.is_free)
    for var, default in PORT_SPECS:
        env_text = set_env(env_text, var, str(assignments[var]))
        if var in errors:
            print(f"ERROR {var} {default}")
        else:
            print(f"PORT {var} {_to_port(current.get(var)) or default} {assignments[var]}")
    env_text, _warnings = sync_derived(env_text, before, assignments)
    _write_env(args.env, env_text)
    for line in _host_notes(host):
        print("NOTE " + line)
    return EXIT_OK


def cmd_check_name(args):
    env = parse_env(_read(args.env)) if os.path.isfile(args.env) else {}
    own_dir = os.path.realpath(args.project_dir)
    try:
        containers, _volumes, networks = docker_state()
    except DockerUnavailable:
        return EXIT_OK
    errors, warnings = name_conflicts(
        args.name,
        _suffixes(own_dir),
        containers,
        networks,
        image_project(args.name),
        own_dir,
        effective_project(env, os.environ, own_dir),
    )
    for line in errors:
        print("ERROR " + line)
    for line in warnings:
        print("WARN " + line)
    return EXIT_CLASH if errors else EXIT_OK


def cmd_project_guard(args):
    env = parse_env(_read(args.env)) if os.path.isfile(args.env) else {}
    own_dir = os.path.realpath(args.project_dir)
    try:
        containers, volumes, networks = docker_state()
    except DockerUnavailable:
        print(GUARD_OK)
        return EXIT_OK
    project = effective_project(env, os.environ, own_dir)
    status, reason = project_guard(
        project,
        _explicit_project(env),
        containers,
        volumes,
        networks,
        own_dir,
        args.fresh,
        args.candidate,
    )
    print(status)
    if status != GUARD_OK:
        print(reason)
        for line in _guard_help(project, manual=status == GUARD_REFUSE):
            print(line)
    return EXIT_OK


def cmd_get(args):
    """Print each KEY's value as Compose reads it ('' when unset)."""
    env = parse_env(_read(args.env)) if os.path.isfile(args.env) else {}
    for key in args.keys:
        print(env.get(key, ""))
    return EXIT_OK


def cmd_set(args):
    """Set KEY=VALUE in .env, keeping every other line byte-for-byte."""
    _write_env(args.env, set_env(_read(args.env), args.key, args.value))
    return EXIT_OK


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def main(argv=None):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = _Parser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-dir", default=root)
    parser.add_argument(
        "--env", default=None, help=".env to read/write (default: <project-dir>/.env)"
    )
    parser.add_argument("--template", default=None, help="default: <project-dir>/.env.example")
    sub = parser.add_subparsers(dest="command", parser_class=_Parser)
    sub.required = True
    sub.add_parser("check").set_defaults(func=cmd_check)
    fix = sub.add_parser("fix")
    fix.add_argument("--yes", action="store_true")
    fix.set_defaults(func=cmd_fix)
    assign = sub.add_parser("assign-ports")
    assign.add_argument("--current", action="append", metavar="VAR=PORT")
    assign.set_defaults(func=cmd_assign_ports)
    name = sub.add_parser("check-name")
    name.add_argument("name")
    name.set_defaults(func=cmd_check_name)
    guard = sub.add_parser("project-guard")
    guard.add_argument("--fresh", action="store_true")
    guard.add_argument("--candidate", default="")
    guard.set_defaults(func=cmd_project_guard)
    get = sub.add_parser("get")
    get.add_argument("keys", nargs="+")
    get.set_defaults(func=cmd_get)
    setter = sub.add_parser("set")
    setter.add_argument("key")
    setter.add_argument("value")
    setter.set_defaults(func=cmd_set)
    args = parser.parse_args(argv)
    args.env = args.env or os.path.join(args.project_dir, ".env")
    args.template = args.template or os.path.join(args.project_dir, ".env.example")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
