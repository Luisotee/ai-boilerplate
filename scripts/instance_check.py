#!/usr/bin/env python3
"""Keep several checkouts of this project from colliding on one host.

Everything host-global is checked against what Docker already knows about the
OTHER compose projects on the machine: published ports, container / network /
image names, and the Compose project name (which owns the volumes).

Standard library only and no imports from the repo, so a fork can copy this one
file. `setup.sh` is a thin caller; run it directly for the same commands:

    python3 scripts/instance_check.py check          # read-only report
    python3 scripts/instance_check.py fix [--yes]    # re-bump clashing ports

Exit codes of `check`: 0 clean, 1 collisions found, 2 Docker state unavailable
(the check was incomplete).
"""

from __future__ import annotations

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


# ── Pure helpers ────────────────────────────────────────────────────────────


def parse_env(text):
    """Active KEY=value lines of a .env file. The first occurrence of a key wins."""
    env = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match and match.group(1) not in env:
            env[match.group(1)] = match.group(2).strip()
    return env


def set_env(text, key, value):
    """Replace every `KEY=` line in place, or append one. Other lines are untouched."""
    pattern = re.compile(r"^%s=.*$" % re.escape(key), re.MULTILINE)
    line = "%s=%s" % (key, value)
    if pattern.search(text):
        return pattern.sub(lambda _m: line, text)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


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
    """"8000" -> [8000]; "8000-8002" -> [8000, 8001, 8002]; anything else -> []."""
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


def partition(containers, own_dir):
    """Split into (ours, foreign) by the compose working directory. A container
    started with plain `docker run` has no label and counts as foreign."""
    own, foreign = [], []
    for container in containers:
        (own if same_dir(container.working_dir, own_dir) else foreign).append(container)
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
    return re.findall(r"container_name:\s*['\"]?\$\{SERVICE_NAME[^}]*\}-([A-Za-z0-9_.-]+)", compose_text)


def name_conflicts(service_name, suffixes, containers, networks, image_project, own_dir, project):
    """Things named after SERVICE_NAME that another compose project already owns.

    Returns (errors, warnings). The image is only a warning: its label records
    the last project that built the tag, which may be long gone.
    """
    errors, warnings = [], []
    wanted = set("%s-%s" % (service_name, suffix) for suffix in suffixes)
    _own, foreign = partition(containers, own_dir)
    for container in foreign:
        clash = container.name in wanted if wanted else container.name.startswith(service_name + "-")
        if clash:
            owner = container.project or "a non-compose container"
            where = " (%s)" % container.working_dir if container.working_dir else ""
            errors.append("container '%s' already exists, owned by %s%s" % (container.name, owner, where))
    network_name = "%s-network" % service_name
    for network in networks:
        if network.name == network_name and network.project != project:
            owner = network.project or "something outside compose"
            errors.append("network '%s' already exists, owned by %s" % (network_name, owner))
    if image_project and image_project != project:
        warnings.append(
            "image '%s-api:latest' was last built by project '%s'; building here replaces that tag"
            % (service_name, image_project)
        )
    return errors, warnings


def project_guard(project, explicit, containers, volumes, networks, own_dir, fresh, candidate):
    """Is the Compose project name (= volume owner) shared with another checkout?

    OK      nothing else uses it, or COMPOSE_PROJECT_NAME is set explicitly.
    OFFER   safe to set COMPOSE_PROJECT_NAME=<candidate>: this is a first setup
            (no .env before, no containers of ours) and the candidate is unused.
    REFUSE  shared, and changing the name here could orphan existing volumes.
    Returns (status, reason).
    """
    if explicit:
        return GUARD_OK, ""
    own, foreign = partition(containers, own_dir)
    foreign_same = [c for c in foreign if c.project == project]
    leftovers = [r.name for r in list(volumes) + list(networks) if r.project == project]
    if foreign_same:
        dirs = sorted(set(c.working_dir for c in foreign_same if c.working_dir))
        reason = "another checkout uses the Compose project name '%s': %s" % (project, ", ".join(dirs) or "unknown directory")
    elif leftovers and not own:
        reason = "volumes/networks of a Compose project named '%s' already exist (%s)" % (
            project,
            ", ".join(sorted(leftovers)[:4]),
        )
    else:
        return GUARD_OK, ""
    used = set(c.project for c in containers) | set(r.project for r in list(volumes) + list(networks))
    if fresh and not own and candidate and candidate != project and candidate not in used:
        return GUARD_OFFER, reason
    return GUARD_REFUSE, reason


def missing_keys(template_text, env_text):
    """Template keys absent from .env, in template order."""
    env = parse_env(env_text)
    return [key for key in parse_env(template_text) if key not in env]


def sync_derived(env_text, old_ports, new_ports):
    """Follow a port change in the local-dev URLs that embed it.

    Only a URL that still has the exact expected shape is rewritten; anything
    hand-edited is left alone and reported. Returns (text, warnings).
    """
    env = parse_env(env_text)
    warnings = []
    old, new = old_ports.get("POSTGRES_PORT"), new_ports.get("POSTGRES_PORT")
    if old and new and old != new and "DATABASE_URL" in env:
        pattern = re.compile(r"@(localhost|127\.0\.0\.1):%d/" % old)
        if pattern.search(env["DATABASE_URL"]):
            env_text = set_env(env_text, "DATABASE_URL", pattern.sub(r"@\g<1>:%d/" % new, env["DATABASE_URL"]))
        else:
            warnings.append("DATABASE_URL does not point at localhost:%d; update it by hand" % old)
    for port_var, url_var in CLIENT_URLS.items():
        old, new = old_ports.get(port_var), new_ports.get(port_var)
        if not old or not new or old == new:
            continue
        if url_var not in env or env[url_var] == "http://localhost:%d" % old:
            env_text = set_env(env_text, url_var, "http://localhost:%d" % new)
        else:
            warnings.append("%s is customised; point it at port %d by hand" % (url_var, new))
    return env_text, warnings


def port_clashes(ports, claims):
    """{var: [Claim, …]} for every configured port someone else claims."""
    by_port = {}
    for claim in claims:
        by_port.setdefault(claim.port, []).append(claim)
    return dict((var, by_port[port]) for var, port in ports.items() if port in by_port)


def describe_claim(claim):
    if claim.source == "listening":
        return "in use by a process on this host"
    state = "container exists" if claim.source == "bound" else "stack not created"
    return "%s (%s, %s)" % (claim.owner, claim.service, state)


# ── Docker / host I/O ───────────────────────────────────────────────────────


class DockerUnavailable(Exception):
    pass


def _run(args, cwd=None, env=None, timeout=15):
    try:
        done = subprocess.run(
            args, cwd=cwd, env=env, timeout=timeout, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerUnavailable(type(exc).__name__)
    if done.returncode != 0:
        lines = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise DockerUnavailable(lines[-1] if lines else "exit %d" % done.returncode)
    return done.stdout.decode("utf-8", "replace")


def _labelled(kind):
    out = _run(["docker", kind, "ls", "--format", '{{.Name}}\t{{.Label "%s"}}' % L_PROJECT])
    rows = [line.split("\t") for line in out.splitlines() if line.strip()]
    return [Resource(row[0], row[1] if len(row) > 1 else "") for row in rows]


def docker_state():
    """(containers, volumes, networks). Raises DockerUnavailable."""
    if not shutil.which("docker"):
        raise DockerUnavailable("docker is not installed")
    ids = _run(["docker", "ps", "-aq"]).split()
    inspected = json.loads(_run(["docker", "inspect"] + ids)) if ids else []
    return parse_containers(inspected), _labelled("volume"), _labelled("network")


def image_project(service_name):
    """Compose project that last built <service_name>-api:latest ('' if none)."""
    fmt = '{{index .Config.Labels "%s"}}' % L_PROJECT
    try:
        out = _run(["docker", "image", "inspect", "%s-api:latest" % service_name, "--format", fmt]).strip()
    except DockerUnavailable:
        return ""
    return "" if out == "<no value>" else out


def sibling_config(directory, config_files):
    """Every port a neighbouring compose project publishes, profiles included.

    The rendered config contains that project's secrets: it is parsed for ports
    and dropped, never printed. Returns None when it cannot be rendered.
    """
    # Only what docker needs: an exported AI_API_PORT or COMPOSE_* in our shell
    # would otherwise override the neighbour's own .env.
    env = dict((k, v) for k, v in os.environ.items() if k in ("PATH", "HOME") or k.startswith("DOCKER_"))
    base = ["docker", "compose"]
    for path in config_files:
        base += ["-f", path]
    for profile in (["--profile", "*"], []):
        try:
            out = _run(base + profile + ["config", "--format", "json"], cwd=directory, env=env, timeout=20)
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


class Host(object):
    """Everything known about the host, gathered once per run."""

    def __init__(self, project_dir):
        self.own_dir = os.path.realpath(project_dir)
        self.containers, self.volumes, self.networks = [], [], []
        self.docker_error = ""
        self.unreadable = []
        try:
            self.containers, self.volumes, self.networks = docker_state()
        except (DockerUnavailable, ValueError) as exc:
            self.docker_error = str(exc) or "unknown error"
        own, foreign = partition(self.containers, self.own_dir)
        self.own_ports = set(port for c in own for port in c.ports)
        claims = claims_from_containers(foreign)
        seen = set((c.port, c.owner) for c in claims)
        if shutil.which("docker"):
            for directory, files in sorted(sibling_dirs(self.own_dir, foreign).items()):
                declared = sibling_config(directory, files)
                if declared is None:
                    self.unreadable.append(directory)
                    continue
                for claim in declared:
                    if (claim.port, claim.owner) not in seen:
                        seen.add((claim.port, claim.owner))
                        claims.append(claim)
        self.claims = claims
        self.claimed = set(c.port for c in claims)

    def is_free(self, port):
        if port in self.own_ports:
            return True
        return port not in self.claimed and not is_listening(port)

    def clashes(self, ports):
        """{var: [Claim]} including ports held by a non-Docker process."""
        found = port_clashes(ports, self.claims)
        for var, port in ports.items():
            if var not in found and port not in self.own_ports and is_listening(port):
                found[var] = [Claim(port, "", "", "listening")]
        return found


# ── Commands ────────────────────────────────────────────────────────────────


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _write_env(path, text):
    tmp = path + ".tmp"
    handle = os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8")
    with handle:
        handle.write(text)
    os.replace(tmp, path)


def _configured_ports(env):
    """The port each variable resolves to: the .env value, else compose's default."""
    return dict((var, _to_port(env.get(var)) or default) for var, default in PORT_SPECS)


def _suffixes(project_dir):
    for name in COMPOSE_FILES:
        path = os.path.join(project_dir, name)
        if os.path.isfile(path):
            return container_suffixes(_read(path))
    return []


def _docker_note(host):
    if not host.docker_error:
        return []
    return [
        "Docker state unavailable (%s): only live sockets and neighbouring compose files were checked"
        % host.docker_error
    ]


def _guard_help(project, manual=True):
    lines = [
        "Both checkouts would share the '%s_*' volumes (database, WhatsApp session, uploads)" % project,
        "and `docker compose up/down` in one would replace the other's containers.",
    ]
    if not manual:
        return lines
    return lines + [
        "Fix by hand: stop the stack, decide which checkout owns those volumes, and set",
        "COMPOSE_PROJECT_NAME=<unique name> in the OTHER checkout's .env (it starts with empty volumes).",
        "If this directory was simply moved or renamed, set COMPOSE_PROJECT_NAME=%s here instead." % project,
    ]


def cmd_check(args):
    env_text = _read(args.env) if os.path.isfile(args.env) else ""
    env = parse_env(env_text)
    host = Host(args.project_dir)
    project = effective_project(env, os.environ, host.own_dir)
    service_name = env.get("SERVICE_NAME") or DEFAULT_SERVICE_NAME
    ports = _configured_ports(env)

    print("Instance check: SERVICE_NAME '%s', Compose project '%s'" % (service_name, project))
    problems = 0

    clashes = host.clashes(ports)
    if clashes:
        print("\nPort clashes:")
        for var, _default in PORT_SPECS:
            for claim in clashes.get(var, []):
                problems += 1
                print("  %s=%d: %s" % (var, claim.port, describe_claim(claim)))

    errors, warnings = name_conflicts(
        service_name,
        _suffixes(host.own_dir),
        host.containers,
        host.networks,
        "" if host.docker_error else image_project(service_name),
        host.own_dir,
        project,
    )
    if errors:
        problems += len(errors)
        print("\nName clashes (choose another SERVICE_NAME in .env):")
        for line in errors:
            print("  " + line)

    status, reason = project_guard(
        project,
        bool(os.environ.get("COMPOSE_PROJECT_NAME") or env.get("COMPOSE_PROJECT_NAME")),
        host.containers,
        host.volumes,
        host.networks,
        host.own_dir,
        False,
        "",
    )
    if status != GUARD_OK:
        problems += 1
        print("\nShared Compose project name: " + reason)
        for line in _guard_help(project):
            print("  " + line)

    notes = list(warnings)
    if os.path.isfile(args.template):
        template_text = _read(args.template)
        defaults = parse_env(template_text)
        # A key the template leaves empty is optional: absent means the same.
        absent = [key for key in missing_keys(template_text, env_text) if defaults[key]]
        if absent:
            notes.append(".env lacks %d key(s) from .env.example (defaults apply): %s" % (len(absent), " ".join(absent)))
    for directory in host.unreadable:
        notes.append("could not read the compose config in %s; its unstarted services are not counted" % directory)
    notes += _docker_note(host)
    if notes:
        print("\nNotes:")
        for line in notes:
            print("  " + line)

    if problems:
        if clashes:
            print("\nRun ./setup.sh --fix to move the clashing ports (nothing else in .env is changed).")
        return 1
    print("\nNo collisions found." if not host.docker_error else "\nNo collisions found in what could be checked.")
    return 2 if host.docker_error else 0


def cmd_fix(args):
    if not os.path.isfile(args.env):
        print("No %s found. Run ./setup.sh first." % args.env)
        return 1
    env_text = _read(args.env)
    env = parse_env(env_text)
    host = Host(args.project_dir)
    current = _configured_ports(env)
    assignments, errors = allocate(PORT_SPECS, current, host.is_free)

    moved = [(var, current[var], assignments[var]) for var, _d in PORT_SPECS if assignments[var] != current[var]]
    pinned = [var for var, _d in PORT_SPECS if var not in env]
    for line in _docker_note(host):
        print(line)
    for var in errors:
        print("No free port near %d for %s: set it by hand in .env" % (dict(PORT_SPECS)[var], var))
    if not moved and not pinned:
        print("Ports are fine; nothing to change.")
        return 1 if errors else 0

    for var, old, new in moved:
        print("  %s: %d -> %d" % (var, old, new))
    if pinned:
        print("  written explicitly (were defaults): %s" % " ".join(pinned))
    if not args.yes:
        try:
            answer = input("Apply to %s? (y/N): " % args.env)
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return 1

    backup = "%s.bak.%s" % (args.env, time.strftime("%Y%m%d-%H%M%S"))
    _write_env(backup, env_text)
    new_text = env_text
    for var, _default in PORT_SPECS:
        if var in pinned or assignments[var] != current[var]:
            new_text = set_env(new_text, var, str(assignments[var]))
    new_text, warnings = sync_derived(new_text, current, assignments)
    _write_env(args.env, new_text)
    print("Updated %s (backup: %s)" % (args.env, backup))
    for line in warnings:
        print("  ! " + line)
    if moved:
        print("Apply with `docker compose up -d`. Anything that targets the old ports from outside")
        print("(reverse proxy, webhook URL, FleetView base URL, firewall rules) must follow.")
    return 1 if errors else 0


def cmd_assign_ports(args):
    """First-time allocation for setup.sh. Prints `VAR default chosen` per port,
    or `ERROR VAR default` when nothing near the default is free."""
    env_text = _read(args.env)
    current = {}
    for pair in args.current or []:
        var, _, value = pair.partition("=")
        current[var] = value
    host = Host(args.project_dir)
    assignments, errors = allocate(PORT_SPECS, current, host.is_free)
    defaults = dict(PORT_SPECS)
    for var, default in PORT_SPECS:
        env_text = set_env(env_text, var, str(assignments[var]))
        if var in errors:
            print("ERROR %s %d" % (var, default))
        else:
            print("%s %d %d" % (var, default, assignments[var]))
    env_text, _warnings = sync_derived(env_text, defaults, assignments)
    _write_env(args.env, env_text)
    for line in _docker_note(host):
        print("NOTE " + line)
    return 0


def cmd_check_name(args):
    env = parse_env(_read(args.env)) if os.path.isfile(args.env) else {}
    own_dir = os.path.realpath(args.project_dir)
    try:
        containers, _volumes, networks = docker_state()
    except (DockerUnavailable, ValueError):
        return 0
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
    return 1 if errors else 0


def cmd_project_guard(args):
    env = parse_env(_read(args.env)) if os.path.isfile(args.env) else {}
    own_dir = os.path.realpath(args.project_dir)
    try:
        containers, volumes, networks = docker_state()
    except (DockerUnavailable, ValueError):
        print(GUARD_OK)
        return 0
    project = effective_project(env, os.environ, own_dir)
    status, reason = project_guard(
        project,
        bool(os.environ.get("COMPOSE_PROJECT_NAME") or env.get("COMPOSE_PROJECT_NAME")),
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
    return 0


def main(argv=None):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-dir", default=root)
    parser.add_argument("--env", default=None, help=".env to read/write (default: <project-dir>/.env)")
    parser.add_argument("--template", default=None, help="default: <project-dir>/.env.example")
    sub = parser.add_subparsers(dest="command")
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
    args = parser.parse_args(argv)
    args.env = args.env or os.path.join(args.project_dir, ".env")
    args.template = args.template or os.path.join(args.project_dir, ".env.example")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
