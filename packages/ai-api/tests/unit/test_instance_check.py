"""scripts/instance_check.py: several bots on one host.

The script is a standalone, stdlib-only file at the repo root, so it is loaded
by path. Fixtures use the shapes `docker inspect` and `docker compose config
--format json` really return. Commands run against a `Host` built from data
(`load_host` is patched), so nothing here touches Docker or real sockets.
"""

import importlib.util
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "instance_check.py"
# Fail loudly rather than skip: a moved script must not make these tests vanish.
assert SCRIPT.is_file(), f"{SCRIPT} is missing"

_spec = importlib.util.spec_from_file_location("instance_check", SCRIPT)
ic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ic)

OWN = "/srv/mybot"
SUFFIXES = ["postgres", "redis", "api", "worker", "whatsapp"]


def _inspect(name, project, workdir, bindings, service="api"):
    labels = {}
    if project:
        labels = {
            ic.L_PROJECT: project,
            ic.L_WORKDIR: workdir,
            ic.L_FILES: f"{workdir}/docker-compose.yml",
            ic.L_SERVICE: service,
        }
    return {
        "Name": f"/{name}",
        "Config": {"Labels": labels},
        "HostConfig": {"PortBindings": bindings},
    }


def _bind(port):
    return {"5432/tcp": [{"HostIp": "", "HostPort": str(port)}]}


def _containers(*items):
    return ic.parse_containers(list(items))


class TestEnvFile:
    def test_parse_follows_compose_dotenv_rules(self):
        text = (
            "# A=1\n"
            "A=2\n"
            "export B=5440\n"
            'C="8000" # api\n'
            "D='x # y'\n"
            "E=9000 # comment\n"
            "F = spaced \n"
            "A=3\n"
            "G=\n"
        )
        assert ic.parse_env(text) == {
            "A": "3",
            "B": "5440",
            "C": "8000",
            "D": "x # y",
            "E": "9000",
            "F": "spaced",
            "G": "",
        }

    def test_parse_crlf(self):
        assert ic.parse_env("A=1\r\nB=2\r\n") == {"A": "1", "B": "2"}

    def test_set_replaces_every_assignment_in_place(self):
        text = "X=1\nAI_API_PORT=8000\n# AI_API_PORT=1\nY=a&b|c\nAI_API_PORT=9\n"
        assert ic.set_env(text, "AI_API_PORT", "8002") == (
            "X=1\nAI_API_PORT=8002\n# AI_API_PORT=1\nY=a&b|c\nAI_API_PORT=8002\n"
        )

    def test_set_keeps_export_quotes_and_crlf(self):
        text = 'export P=5440\r\nQ="8000" # api\r\nR=1\r\n'
        text = ic.set_env(ic.set_env(text, "P", "5441"), "Q", "8001")
        assert text == 'export P=5441\r\nQ="8001" # api\r\nR=1\r\n'
        assert ic.set_env(text, "NEW", "1").endswith("R=1\r\nNEW=1\r\n")

    def test_set_appends_when_missing(self):
        assert ic.set_env("X=1", "P", "2") == "X=1\nP=2\n"
        assert ic.set_env("", "P", "2") == "P=2\n"

    def test_set_keeps_an_inline_comment(self):
        assert ic.set_env("X=1  # note\n", "X", "2") == "X=2  # note\n"

    def test_set_quotes_a_value_that_would_misparse(self):
        text = ic.set_env("X=1\n", "X", "a # b")
        assert text == "X='a # b'\n" and ic.parse_env(text) == {"X": "a # b"}

    def test_set_quotes_a_dollar_so_compose_does_not_interpolate(self):
        assert ic.set_env("U=\n", "U", "pg://a:p$w@h/db") == "U='pg://a:p$w@h/db'\n"

    def test_set_value_is_literal(self):
        assert ic.set_env("U=old\n", "U", r"a\1b") == "U=a\\1b\n"

    def test_missing_keys_in_template_order(self):
        assert ic.missing_keys("A=1\nB=\nC=3\n", "B=x\nexport C=4\n") == ["A"]


class TestProjectName:
    @pytest.mark.parametrize(
        ("basename", "expected"),
        [("ai-boilerplate", "ai-boilerplate"), ("My.App", "myapp"), ("_-bot 2", "bot2")],
    )
    def test_normalize(self, basename, expected):
        assert ic.normalize_project_name(basename) == expected

    def test_shell_beats_env_beats_directory(self):
        assert ic.effective_project({}, {}, "/srv/My.Bot/") == "mybot"
        assert ic.effective_project({"COMPOSE_PROJECT_NAME": "e"}, {}, "/srv/x") == "e"
        env, shell = {"COMPOSE_PROJECT_NAME": "e"}, {"COMPOSE_PROJECT_NAME": "s"}
        assert ic.effective_project(env, shell, "/srv/x") == "s"


class TestParsing:
    def test_inspect_bindings(self):
        containers = _containers(
            _inspect(
                "a-api",
                "a",
                "/srv/a",
                {
                    "8000/tcp": [{"HostIp": "", "HostPort": "8001"}],
                    "53/udp": [{"HostIp": "", "HostPort": "53"}],
                    "9000/tcp": [{"HostIp": "", "HostPort": ""}],
                    "2377/tcp": None,
                    "7000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "7000-7001"}],
                },
            ),
            _inspect("plain", "", "", None),
        )
        assert containers[0].ports == [7000, 7001, 8001]
        assert containers[0].config_files == ["/srv/a/docker-compose.yml"]
        assert containers[1].ports == []
        assert containers[1].project == ""

    def test_config_ports_including_profiles_and_ranges(self):
        config = {
            "name": "curupira-bot",
            "services": {
                "api": {
                    "ports": [
                        {"mode": "ingress", "target": 8000, "published": "8000", "protocol": "tcp"}
                    ]
                },
                "telegram": {
                    "profiles": ["telegram"],
                    "ports": [{"target": 3003, "published": "3003-3004", "protocol": "tcp"}],
                },
                "dns": {"ports": [{"target": 53, "published": "53", "protocol": "udp"}]},
                "internal": {"ports": [{"target": 9000, "protocol": "tcp"}]},
                "worker": {"ports": None},
                "legacy": {"ports": [{"target": 1, "published": 8085}]},
            },
        }
        claims = ic.ports_from_config(config)
        assert sorted(c.port for c in claims) == [3003, 3004, 8000, 8085]
        assert {c.owner for c in claims} == {"curupira-bot"}
        assert {c.source for c in claims} == {"declared"}

    def test_config_without_name_uses_fallback(self):
        claims = ic.ports_from_config(
            {"services": {"a": {"ports": [{"published": "1234"}]}}}, "dir"
        )
        assert claims == [ic.Claim(1234, "dir", "a", "declared")]

    def test_container_suffixes(self):
        text = (
            "container_name: ${SERVICE_NAME:-aiagent}-postgres\n"
            "  container_name: ${SERVICE_NAME:-aiagent}-whisper-init\n"
        )
        assert ic.container_suffixes(text) == ["postgres", "whisper-init"]
        assert ic.container_suffixes("container_name: curupira-api") == []


class TestPartition:
    def test_same_project_name_in_another_directory_is_foreign(self, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        containers = _containers(
            _inspect("mine", "bot", OWN, {}),
            _inspect("theirs", "bot", str(other), {}),
            _inspect("plain", "", "", {}),
        )
        own, foreign = ic.partition(containers, OWN + "/", "bot")
        assert [c.name for c in own] == ["mine"]
        assert [c.name for c in foreign] == ["theirs", "plain"]

    def test_moved_checkout_keeps_its_containers(self):
        containers = _containers(
            _inspect("mine", "bot", "/gone/old-path", {}),
            _inspect("theirs", "other", "/gone/elsewhere", {}),
        )
        own, foreign = ic.partition(containers, OWN, "bot", exists=lambda _p: False)
        assert [c.name for c in own] == ["mine"]
        assert [c.name for c in foreign] == ["theirs"]
        # A directory that still exists belongs to another checkout, even with our name.
        own, _ = ic.partition(containers, OWN, "bot", exists=lambda _p: True)
        assert own == []


class TestAllocate:
    SPECS = [("A", 5432), ("B", 6379), ("C", 5433)]

    def test_defaults_when_everything_is_free(self):
        assert ic.allocate(self.SPECS, {}, lambda p: True) == (
            {"A": 5432, "B": 6379, "C": 5433},
            [],
        )

    def test_skips_taken_and_never_assigns_twice(self):
        assignments, errors = ic.allocate(self.SPECS, {}, lambda p: p != 5432)
        # A is bumped onto 5433, so C (default 5433) must move on.
        assert assignments == {"A": 5433, "B": 6379, "C": 5434}
        assert errors == []

    def test_keeps_current_port_when_free(self):
        assignments, _ = ic.allocate(self.SPECS, {"A": "5440", "B": "junk"}, lambda p: True)
        assert assignments == {"A": 5440, "B": 6379, "C": 5433}

    def test_current_port_is_reserved_before_others_scan(self):
        assignments, _ = ic.allocate(self.SPECS, {"C": "5433"}, lambda p: p != 5432)
        assert assignments == {"A": 5434, "B": 6379, "C": 5433}

    def test_clashing_current_is_rebumped_from_the_default(self):
        assignments, _ = ic.allocate([("A", 8000)], {"A": "8000"}, lambda p: p not in {8000, 8001})
        assert assignments == {"A": 8002}

    def test_exhaustion_is_reported_and_value_kept(self):
        assignments, errors = ic.allocate([("A", 8000)], {"A": "8000"}, lambda p: False, span=3)
        assert errors == ["A"]
        assert assignments == {"A": 8000}


def _host(containers=(), declared=(), listening=(), **kwargs):
    busy = set(listening)
    return ic.Host(
        OWN,
        kwargs.pop("project", "mybot"),
        _containers(*containers),
        declared=declared,
        listening=lambda port: port in busy,
        **kwargs,
    )


class TestHost:
    def test_port_shared_with_another_project_is_not_free_even_if_ours(self):
        host = _host(
            [
                _inspect("mybot-postgres", "mybot", OWN, _bind(5432)),
                _inspect("x-db", "x", "/srv/x", _bind(5432)),
            ],
            listening=[5432],
        )
        assert host.is_free(5432) is False
        assert list(host.clashes({"POSTGRES_PORT": 5432})) == ["POSTGRES_PORT"]

    def test_our_own_listening_port_is_free(self):
        host = _host([_inspect("mybot-postgres", "mybot", OWN, _bind(5432))], listening=[5432])
        assert host.is_free(5432) is True
        assert host.clashes({"POSTGRES_PORT": 5432}) == {}

    def test_declared_and_listening_ports_are_taken(self):
        host = _host(declared=[ic.Claim(8000, "curupira-bot", "api", "declared")], listening=[3001])
        assert host.is_free(8000) is False
        assert host.is_free(3001) is False
        assert host.is_free(3002) is True
        found = host.clashes({"AI_API_PORT": 8000, "WHATSAPP_API_PORT": 3001})
        assert found["WHATSAPP_API_PORT"][0].source == "listening"

    def test_assumed_own_ports_are_free_but_claims_still_win(self):
        host = _host(declared=[ic.Claim(8000, "c", "api", "declared")], listening=[5432, 8000])
        host.assumed_own = {5432, 8000}
        assert host.is_free(5432) is True
        assert host.is_free(8000) is False

    def test_incomplete(self):
        assert _host().incomplete is False
        assert _host(docker_error="down").incomplete is True
        assert _host(unreadable=["/srv/x"]).incomplete is True


class TestClashes:
    def test_reports_every_owner_of_a_configured_port(self):
        claims = [
            ic.Claim(3003, "castanha-bot", "whatsapp", "bound"),
            ic.Claim(3003, "curupira-bot", "telegram", "declared"),
            ic.Claim(9999, "x", "y", "bound"),
        ]
        found = ic.port_clashes({"TELEGRAM_PORT": 3003, "AI_API_PORT": 8000}, claims)
        assert list(found) == ["TELEGRAM_PORT"]
        assert [ic.describe_claim(c) for c in found["TELEGRAM_PORT"]] == [
            "castanha-bot (whatsapp, container exists)",
            "curupira-bot (telegram, stack not created)",
        ]


class TestNameConflicts:
    def _run(self, name, containers, networks=(), image="", project="mybot"):
        return ic.name_conflicts(name, SUFFIXES, containers, list(networks), image, OWN, project)

    def test_foreign_container_with_our_name(self):
        containers = _containers(_inspect("castanha-api", "castanha-bot", "/srv/castanha", {}))
        errors, warnings = self._run("castanha", containers)
        assert len(errors) == 1 and "castanha-api" in errors[0] and "castanha-bot" in errors[0]
        assert warnings == []

    def test_own_containers_and_lookalikes_are_fine(self):
        containers = _containers(
            _inspect("mybot-api", "mybot", OWN, {}),
            # SERVICE_NAME "ai" must not trip on another project's "ai-boilerplate-…".
            _inspect("ai-boilerplate-whatsapp-1", "ai-boilerplate", "/srv/ai", {}),
        )
        assert self._run("mybot", containers) == ([], [])
        assert self._run("ai", containers) == ([], [])

    def test_prefix_match_when_compose_has_no_suffixes(self):
        containers = _containers(_inspect("x-anything", "", "", {}))
        errors, _ = ic.name_conflicts("x", [], containers, [], "", OWN, "mybot")
        assert len(errors) == 1

    def test_network_owned_by_another_project(self):
        errors, _ = self._run("aiagent", [], [ic.Resource("aiagent-network", "other")])
        assert errors == ["network 'aiagent-network' already exists, owned by other"]
        assert self._run("aiagent", [], [ic.Resource("aiagent-network", "mybot")]) == ([], [])

    def test_image_is_only_a_warning(self):
        errors, warnings = self._run("aiagent", [], image="curupira-bot")
        assert errors == [] and len(warnings) == 1
        assert self._run("aiagent", [], image="mybot") == ([], [])


class TestProjectGuard:
    THEIRS = _inspect("other-api", "bot", "/", {})  # "/" always exists: not a moved checkout
    OURS = _inspect("my-api", "bot", OWN, {})
    LEFTOVER = [ic.Resource("bot_postgres-data", "bot")]

    def _run(self, containers=(), volumes=(), fresh=True, candidate="mybot", explicit=False):
        return ic.project_guard(
            "bot", explicit, _containers(*containers), list(volumes), [], OWN, fresh, candidate
        )

    def test_ok_when_nobody_else_uses_the_name(self):
        assert self._run() == (ic.GUARD_OK, "")
        assert self._run(containers=[self.OURS], volumes=self.LEFTOVER) == (ic.GUARD_OK, "")

    def test_ok_when_name_is_set_explicitly(self):
        assert self._run(containers=[self.THEIRS], explicit=True)[0] == ic.GUARD_OK

    def test_foreign_containers_offer_only_on_a_first_setup(self):
        assert self._run(containers=[self.THEIRS])[0] == ic.GUARD_OFFER
        assert self._run(containers=[self.THEIRS], fresh=False)[0] == ic.GUARD_REFUSE

    def test_leftovers_after_compose_down_are_assumed_ours(self):
        status, note = self._run(volumes=self.LEFTOVER, fresh=False)
        assert status == ic.GUARD_OK
        assert "stopped stack" in note
        # With no .env yet they may be another checkout's: offer, never write silently.
        assert self._run(volumes=self.LEFTOVER)[0] == ic.GUARD_OFFER

    def test_refuse_when_we_already_have_containers(self):
        assert self._run(containers=[self.THEIRS, self.OURS])[0] == ic.GUARD_REFUSE

    def test_refuse_when_candidate_is_unusable(self):
        assert self._run(containers=[self.THEIRS], candidate="bot")[0] == ic.GUARD_REFUSE
        assert self._run(containers=[self.THEIRS], candidate="")[0] == ic.GUARD_REFUSE
        taken = [ic.Resource("mybot_pg", "mybot")]
        assert self._run(containers=[self.THEIRS], volumes=taken)[0] == ic.GUARD_REFUSE


class TestSyncDerived:
    ENV = (
        "DATABASE_URL=postgresql://u:p@localhost:5432/db\n"
        "WHATSAPP_CLIENT_URL=http://localhost:3001\n"
        "TELEGRAM_CLIENT_URL=https://bot.example.com\n"
        "SECRET=keep:5432/\n"
    )

    def test_rewrites_only_urls_in_the_expected_shape(self):
        old = {"POSTGRES_PORT": 5432, "WHATSAPP_API_PORT": 3001, "TELEGRAM_PORT": 3003}
        new = {"POSTGRES_PORT": 5434, "WHATSAPP_API_PORT": 3005, "TELEGRAM_PORT": 3007}
        text, warnings = ic.sync_derived(self.ENV, old, new)
        assert text == (
            "DATABASE_URL=postgresql://u:p@localhost:5434/db\n"
            "WHATSAPP_CLIENT_URL=http://localhost:3005\n"
            "TELEGRAM_CLIENT_URL=https://bot.example.com\n"
            "SECRET=keep:5432/\n"
        )
        assert len(warnings) == 1 and "TELEGRAM_CLIENT_URL" in warnings[0]
        assert "example.com" not in warnings[0]

    def test_unchanged_ports_touch_nothing(self):
        ports = {"POSTGRES_PORT": 5432, "WHATSAPP_API_PORT": 3001}
        assert ic.sync_derived(self.ENV, ports, dict(ports)) == (self.ENV, [])

    def test_database_url_on_another_host_is_left_with_a_warning(self):
        env = "DATABASE_URL=postgresql://u:secret@db.internal:5432/db\n"
        text, warnings = ic.sync_derived(env, {"POSTGRES_PORT": 5432}, {"POSTGRES_PORT": 5434})
        assert text == env
        assert len(warnings) == 1 and "secret" not in warnings[0]

    def test_missing_client_url_is_added(self):
        text, _ = ic.sync_derived("X=1\n", {"WHATSAPP_API_PORT": 3001}, {"WHATSAPP_API_PORT": 3005})
        assert text == "X=1\nWHATSAPP_CLIENT_URL=http://localhost:3005\n"


# ── Commands, against an injected host ─────────────────────────────────────

# The boilerplate's own ports; the `project` fixture declares them in a compose file.
SPECS = [
    ("POSTGRES_PORT", 5432),
    ("REDIS_PORT", 6379),
    ("ADMINER_PORT", 8080),
    ("AI_API_PORT", 8000),
    ("WHATSAPP_API_PORT", 3001),
    ("WHATSAPP_CLOUD_PORT", 3002),
    ("TELEGRAM_PORT", 3003),
    ("WHISPER_PORT", 8771),
]
SERVICES = [
    "postgres",
    "redis",
    "adminer",
    "api",
    "whatsapp",
    "whatsapp-cloud",
    "telegram",
    "whisper",
]
COMPOSE = "services:\n" + "".join(
    f"  {service}:\n"
    f"    container_name: ${{SERVICE_NAME:-aiagent}}-{service}\n"
    f"    ports:\n      - '127.0.0.1:${{{var}:-{port}}}:{port}'\n"
    for service, (var, port) in zip(SERVICES, SPECS, strict=True)
)
ALL_PORTS = "".join(f"{var}={default}\n" for var, default in SPECS)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A checkout directory with a .env and template; Docker-free commands."""
    (tmp_path / ".env.example").write_text("SERVICE_NAME=aiagent\n" + ALL_PORTS)
    (tmp_path / "docker-compose.yml").write_text(COMPOSE)
    monkeypatch.setattr(ic, "image_project", lambda _name: "")
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)

    monkeypatch.setattr(
        ic, "load_host", lambda _d, _p: ic.Host(str(tmp_path), listening=lambda _x: False)
    )
    return tmp_path


@pytest.fixture
def use_host(monkeypatch):
    """Make every command see the given Host."""

    def use(host):
        monkeypatch.setattr(ic, "load_host", lambda _dir, _project: host)

    return use


def _main(project, *argv):
    return ic.main(["--project-dir", str(project), *argv])


def _foreign_on(port):
    return ic.Claim(port, "castanha-bot", "api", "bound")


class TestCmdCheck:
    def test_clean(self, project, capsys):
        (project / ".env").write_text(ALL_PORTS)
        assert _main(project, "check") == ic.EXIT_OK
        assert "No collisions found." in capsys.readouterr().out

    def test_collision(self, project, use_host, capsys):
        (project / ".env").write_text('export AI_API_PORT="8001" # moved\n')
        use_host(ic.Host(str(project), declared=[_foreign_on(8001)], listening=lambda _p: False))
        assert _main(project, "check") == ic.EXIT_CLASH
        assert "AI_API_PORT=8001: castanha-bot (api, container exists)" in capsys.readouterr().out

    def test_incomplete(self, project, use_host, capsys):
        (project / ".env").write_text(ALL_PORTS)
        use_host(ic.Host(str(project), unreadable=["/srv/x"], listening=lambda _p: False))
        assert _main(project, "check") == ic.EXIT_INCOMPLETE
        assert "incomplete" in capsys.readouterr().out

    def test_usage_error(self, project):
        with pytest.raises(SystemExit) as exc:
            _main(project, "no-such-command")
        assert exc.value.code == ic.EXIT_USAGE


class TestCmdFix:
    def test_moves_a_port_our_own_container_shares(self, project, use_host):
        ours = _inspect("mybot-postgres", project.name, str(project), _bind(5432))
        use_host(
            ic.Host(
                str(project),
                project.name,
                _containers(ours),
                declared=[_foreign_on(5432)],
                listening=lambda p: p == 5432,
            )
        )
        (project / ".env").write_text(
            ALL_PORTS + "DATABASE_URL=postgresql://u:p@localhost:5432/db\n"
        )
        assert _main(project, "fix", "--yes") == ic.EXIT_OK
        env = ic.parse_env((project / ".env").read_text())
        assert env["POSTGRES_PORT"] == "5433"
        assert env["DATABASE_URL"] == "postgresql://u:p@localhost:5433/db"

    def test_backup_is_byte_identical_and_crlf_survives(self, project, use_host):
        original = (
            "export AI_API_PORT=8001\n" + ALL_PORTS.replace("AI_API_PORT=8000\n", "")
        ).replace("\n", "\r\n")
        (project / ".env").write_bytes(original.encode())
        use_host(ic.Host(str(project), declared=[_foreign_on(8001)], listening=lambda _p: False))
        assert _main(project, "fix", "--yes") == ic.EXIT_OK
        [backup] = project.glob(".env.bak.*")
        assert backup.read_bytes() == original.encode()
        assert oct(backup.stat().st_mode & 0o777) == "0o600"
        written = (project / ".env").read_bytes()
        assert b"export AI_API_PORT=8000\r\n" in written
        assert b"\n" not in written.replace(b"\r\n", b"")

    def test_symlinked_env_stays_a_symlink(self, project, use_host, tmp_path_factory):
        real = tmp_path_factory.mktemp("secrets") / "bot.env"
        real.write_text(ALL_PORTS.replace("AI_API_PORT=8000", "AI_API_PORT=8001"))
        (project / ".env").symlink_to(real)
        use_host(ic.Host(str(project), declared=[_foreign_on(8001)], listening=lambda _p: False))
        assert _main(project, "fix", "--yes") == ic.EXIT_OK
        assert (project / ".env").is_symlink()
        assert "AI_API_PORT=8000" in real.read_text()

    def test_refuses_without_docker(self, project, use_host, capsys):
        text = ALL_PORTS
        (project / ".env").write_text(text)
        use_host(ic.Host(str(project), docker_error="daemon down", listening=lambda _p: True))
        assert _main(project, "fix", "--yes") == ic.EXIT_INCOMPLETE
        assert (project / ".env").read_text() == text
        assert list(project.glob(".env.bak.*")) == []
        assert "Refusing" in capsys.readouterr().out

    def test_export_line_is_rewritten_not_duplicated(self, project, use_host):
        (project / ".env").write_text(
            ALL_PORTS.replace("POSTGRES_PORT=5432", "export POSTGRES_PORT=5440")
        )
        use_host(ic.Host(str(project), declared=[_foreign_on(5440)], listening=lambda _p: False))
        assert _main(project, "fix", "--yes") == ic.EXIT_OK
        text = (project / ".env").read_text()
        assert text.count("POSTGRES_PORT=") == 1
        assert "export POSTGRES_PORT=5432" in text

    def test_nothing_to_do(self, project, capsys):
        (project / ".env").write_text(ALL_PORTS)
        assert _main(project, "fix", "--yes") == ic.EXIT_OK
        assert "nothing to change" in capsys.readouterr().out
        assert list(project.glob(".env.bak.*")) == []


class TestProjectSpecs:
    def test_reads_host_ports_and_default_name(self):
        text = (
            "    container_name: ${SERVICE_NAME:-castanha}-api\n"
            "      - '127.0.0.1:${POSTGRES_PORT:-5433}:5432'\n"
            '      - "${AI_API_BIND:-}:${AI_API_PORT:-8001}:8000"\n'
            "      WHATSAPP_API_PORT: 3001\n"
            "      - '${WHATSAPP_API_BIND:-127.0.0.1}:${WHATSAPP_API_PORT:-3003}:3001'\n"
            "      - '127.0.0.1:${POSTGRES_PORT:-5433}:5432'\n"
        )
        assert ic.specs_from_compose(text) == (
            [("POSTGRES_PORT", 5433), ("AI_API_PORT", 8001), ("WHATSAPP_API_PORT", 3003)],
            "castanha",
        )

    def test_nothing_declared(self):
        assert ic.specs_from_compose("services: {}\n") == (None, None)

    def test_this_repo_compose_agrees_with_env_example(self):
        """The template pins every compose port at its compose default (forks too)."""
        root = SCRIPT.parents[1]
        specs, name = ic.project_specs(str(root))
        env = ic.parse_env((root / ".env.example").read_text())
        assert name == env["SERVICE_NAME"]
        assert {var: str(port) for var, port in specs} == {var: env.get(var) for var, _ in specs}

    def test_fixture_compose_declares_the_boilerplate_ports(self, project):
        assert ic.project_specs(str(project)) == (SPECS, "aiagent")

    def test_no_compose_file_means_nothing_declared(self, tmp_path):
        assert ic.project_specs(str(tmp_path)) == ([], "")

    def test_fix_and_assign_refuse_without_a_compose_file(self, project, capsys):
        (project / "docker-compose.yml").unlink()
        (project / ".env").write_text(ALL_PORTS)
        assert _main(project, "fix", "--yes") == ic.EXIT_INCOMPLETE
        assert _main(project, "assign-ports") == ic.EXIT_INCOMPLETE
        assert (project / ".env").read_text() == ALL_PORTS
        assert "TELEGRAM_PORT" not in capsys.readouterr().out
        assert _main(project, "check") == ic.EXIT_INCOMPLETE

    def test_fix_uses_the_fork_ports_and_only_their_urls(self, project, use_host, capsys):
        (project / "docker-compose.yml").write_text(
            "      - '${WHATSAPP_API_BIND:-127.0.0.1}:${WHATSAPP_API_PORT:-3003}:3001'\n"
            "      - '${AI_API_BIND:-}:${AI_API_PORT:-8001}:8000'\n"
        )
        (project / ".env").write_text("AI_API_PORT=8001\n")
        use_host(ic.Host(str(project), declared=[_foreign_on(3003)], listening=lambda _p: False))
        assert _main(project, "fix", "--yes") == ic.EXIT_OK
        env = ic.parse_env((project / ".env").read_text())
        assert env == {
            "AI_API_PORT": "8001",
            "WHATSAPP_API_PORT": "3004",
            "WHATSAPP_CLIENT_URL": "http://localhost:3004",
        }


class TestCmdAssignPorts:
    def test_output_format_setup_sh_parses(self, project, use_host, capsys):
        (project / ".env").write_text((project / ".env.example").read_text())
        use_host(ic.Host(str(project), declared=[_foreign_on(3003)], listening=lambda _p: False))
        assert _main(project, "assign-ports", "--current", "AI_API_PORT=9000") == ic.EXIT_OK
        lines = capsys.readouterr().out.splitlines()
        assert "PORT AI_API_PORT 9000 9000" in lines
        assert "PORT TELEGRAM_PORT 3003 3004" in lines
        assert "PORT POSTGRES_PORT 5432 5432" in lines
        assert all(line.split()[0] in ("PORT", "ERROR", "NOTE") for line in lines)
        env = ic.parse_env((project / ".env").read_text())
        assert env["AI_API_PORT"] == "9000" and env["TELEGRAM_PORT"] == "3004"

    def test_without_docker_carried_ports_are_presumed_ours(self, project, use_host, capsys):
        (project / ".env").write_text((project / ".env.example").read_text())
        use_host(ic.Host(str(project), docker_error="down", listening=lambda p: p in (9000, 8000)))
        _main(project, "assign-ports", "--current", "AI_API_PORT=9000")
        out = capsys.readouterr().out
        assert "PORT AI_API_PORT 9000 9000" in out
        assert "NOTE Docker state unavailable" in out

    def test_without_docker_and_no_carried_ports_the_defaults_stay(self, project, use_host, capsys):
        (project / ".env").write_text("SERVICE_NAME=aiagent\n")
        use_host(ic.Host(str(project), docker_error="down", listening=lambda p: p in (8000, 3001)))
        _main(project, "assign-ports")
        out = capsys.readouterr().out
        assert "PORT AI_API_PORT 8000 8000" in out and "PORT WHATSAPP_API_PORT 3001 3001" in out


class TestGetSet:
    def test_get_and_set(self, project, capsys):
        env = project / ".env"
        env.write_bytes(b'SERVICE_NAME="mybot" # name\r\nX=1\r\n')
        _main(project, "get", "SERVICE_NAME", "MISSING")
        assert capsys.readouterr().out == "mybot\n\n"
        _main(project, "set", "AI_API_BIND", "127.0.0.1")
        assert (
            env.read_bytes() == b'SERVICE_NAME="mybot" # name\r\nX=1\r\nAI_API_BIND=127.0.0.1\r\n'
        )


class TestSiblingConfig:
    def test_runs_in_the_sibling_dir_with_a_scrubbed_environment(self, monkeypatch):
        calls = []

        def fake_run(args, cwd=None, env=None, timeout=15):
            calls.append((args, cwd, env))
            return '{"name": "sib", "services": {"api": {"ports": [{"published": "8000"}]}}}'

        monkeypatch.setattr(ic, "_run", fake_run)
        monkeypatch.setenv("AI_API_PORT", "1")
        monkeypatch.setenv("COMPOSE_PROJECT_NAME", "mine")
        monkeypatch.setenv("DOCKER_HOST", "unix:///x")
        claims = ic.sibling_config("/srv/sib", ["/srv/sib/docker-compose.yml"])
        assert claims == [ic.Claim(8000, "sib", "api", "declared")]
        args, cwd, env = calls[0]
        assert cwd == "/srv/sib"
        assert args[:4] == ["docker", "compose", "-f", "/srv/sib/docker-compose.yml"]
        assert ["--profile", "*"] == args[4:6]
        assert "--no-env-resolution" in args
        assert "AI_API_PORT" not in env and "COMPOSE_PROJECT_NAME" not in env
        assert env["DOCKER_HOST"] == "unix:///x"
        assert set(env) <= {"PATH", "HOME"} | {k for k in env if k.startswith("DOCKER_")}

    def test_falls_back_without_profiles_then_gives_up(self, monkeypatch):
        seen = []

        def failing(args, cwd=None, env=None, timeout=15):
            seen.append("--profile" in args)
            raise ic.DockerUnavailable("boom")

        monkeypatch.setattr(ic, "_run", failing)
        assert ic.sibling_config("/srv/sib", []) is None
        assert seen == [True, False]


def test_write_keeps_secrets_private(tmp_path):
    path = tmp_path / ".env"
    ic._write_env(str(path), "A=1\n")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert os.listdir(tmp_path) == [".env"]


def test_write_keeps_the_existing_mode(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\n")
    path.chmod(0o640)
    ic._write_env(str(path), "A=2\n")
    assert oct(os.stat(path).st_mode & 0o777) == "0o640" and path.read_text() == "A=2\n"


def test_latin1_env_round_trips(tmp_path):
    path = tmp_path / ".env"
    original = "# configura\xe7\xe3o\nAI_API_PORT=8000\n".encode("latin-1")
    path.write_bytes(original)
    ic._write_env(str(path), ic.set_env(ic._read(str(path)), "AI_API_PORT", "8002"))
    assert path.read_bytes() == original.replace(b"8000", b"8002")


def test_backup_never_overwrites_and_is_private(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"S=1\n")
    first, second = ic._backup(str(path)), ic._backup(str(path))
    assert first != second
    for backup in (first, second):
        assert open(backup, "rb").read() == b"S=1\n"
        assert oct(os.stat(backup).st_mode & 0o777) == "0o600"


class TestCarry:
    def test_values_lines_and_extras_are_kept(self):
        template = "# db\nPOSTGRES_PASSWORD=\nAI_API_PORT=8000\nNEW_KEY=default\nEMPTY=\n"
        old = (
            "POSTGRES_PASSWORD='pa$$ # x'\n"
            "export AI_API_PORT=8002\n"
            "EMPTY=\n"
            "WHITELIST_PHONES=5511999\n"
            "DATABASE_URL=postgresql://a:b@localhost:5432/c\n"
            "OLD_ONLY=1 # note\n"
        )
        text, kept, appended = ic.carry_env(template, old, skip=("DATABASE_URL",))
        assert (kept, appended) == (2, 2)
        assert text == (
            "# db\nPOSTGRES_PASSWORD='pa$$ # x'\nexport AI_API_PORT=8002\nNEW_KEY=default\nEMPTY=\n"
            "\n# Kept from the previous .env (not in .env.example)\n"
            "WHITELIST_PHONES=5511999\nOLD_ONLY=1 # note\n"
        )
        assert ic.parse_env(text)["POSTGRES_PASSWORD"] == "pa$$ # x"

    def test_crlf_template_and_last_assignment_wins(self):
        text, kept, appended = ic.carry_env("A=\r\nB=x\r\n", "A=1\nA=2\n")
        assert text == "A=2\r\nB=x\r\n" and (kept, appended) == (1, 0)

    def test_command(self, project, capsys):
        (project / ".env").write_text("A=\nB=\n")
        previous = project / ".env.bak.1"
        previous.write_text("A=1\nC=3\n")
        assert _main(project, "carry", str(previous)) == ic.EXIT_OK
        assert capsys.readouterr().out == "1 1\n"
        assert ic.parse_env((project / ".env").read_text()) == {"A": "1", "B": "", "C": "3"}


class TestComposeText:
    def test_port_services(self):
        assert ic.port_services(COMPOSE)["WHATSAPP_API_PORT"] == "whatsapp"
        assert ic.port_services(COMPOSE)["POSTGRES_PORT"] == "postgres"
        assert ic.port_services("x:\n  a:\n    - '${A_PORT:-1}:1'\n") == {}

    def test_network_check_follows_the_compose_file(self):
        assert ic.names_network("networks:\n  n:\n    name: ${SERVICE_NAME:-aiagent}-network\n")
        assert not ic.names_network("networks:\n  curupira-network:\n    driver: bridge\n")
        owned = [ic.Resource("mybot-network", "other")]
        assert ic.name_conflicts("mybot", [], [], owned, "", OWN, "p", network=False) == ([], [])
        assert ic.name_conflicts("mybot", [], [], owned, "", OWN, "p")[0]


class TestCheckEdges:
    def test_pending_move_of_a_running_container(self, project, use_host, capsys):
        (project / ".env").write_text(
            ALL_PORTS.replace("WHATSAPP_API_PORT=3001", "WHATSAPP_API_PORT=3005")
        )
        ours = _inspect("aiagent-whatsapp", project.name, str(project), _bind(3001), "whatsapp")
        use_host(ic.Host(str(project), project.name, _containers(ours), listening=lambda _p: False))
        assert _main(project, "check") == ic.EXIT_OK
        out = capsys.readouterr().out
        assert (
            "WHATSAPP_API_PORT=3005, but this checkout's 'whatsapp' container publishes 3001" in out
        )

    def test_without_docker_listening_ports_are_not_collisions(self, project, use_host, capsys):
        (project / ".env").write_text(ALL_PORTS)
        use_host(ic.Host(str(project), docker_error="down", listening=lambda p: p == 8000))
        assert _main(project, "check") == ic.EXIT_INCOMPLETE
        out = capsys.readouterr().out
        assert "possibly this checkout's own stack" in out and "AI_API_PORT=8000" in out
        assert "--fix" not in out

    def test_without_docker_a_declared_clash_still_counts(self, project, use_host):
        (project / ".env").write_text(ALL_PORTS)
        host = ic.Host(str(project), declared=[_foreign_on(8000)], docker_error="down")
        use_host(host)
        assert _main(project, "check") == ic.EXIT_CLASH

    @pytest.mark.parametrize("name", ["bot_", "a__b", "Bot", "a-", "a b"])
    def test_invalid_service_names(self, project, name, capsys):
        assert _main(project, "check-name", name) == ic.EXIT_CLASH
        assert capsys.readouterr().out.startswith("ERROR ")

    def test_fix_warns_when_database_url_is_not_set(self, project, use_host, capsys):
        (project / ".env").write_text(ALL_PORTS)
        use_host(ic.Host(str(project), declared=[_foreign_on(5432)], listening=lambda _p: False))
        assert _main(project, "fix", "--yes") == ic.EXIT_OK
        assert (
            "DATABASE_URL is not set; local (non-Docker) dev must use port 5433"
            in capsys.readouterr().out
        )


TEMPLATE = (
    "# === Identity ===\n"
    "SERVICE_NAME=aiagent\n"
    "# COMPOSE_PROJECT_NAME=\n"
    "\n"
    "# Host address each app port is published on.\n"
    "AI_API_BIND=\n"
    "WHATSAPP_API_BIND=\n"
    "\n"
    "# New provider key (https://example.com)\n"
    "NEWLLM_API_KEY=\n"
    "# Its model\n"
    "NEWLLM_MODEL=fast-1  # default model\n"
)


class TestAddMissing:
    def test_appends_in_template_order_with_one_comment_per_group(self):
        env = "SERVICE_NAME=mybot\nAI_API_BIND=127.0.0.1\n"
        text, added = ic.add_missing(env, TEMPLATE, stamp="2026-10-02")
        assert added == ["WHATSAPP_API_BIND", "NEWLLM_API_KEY", "NEWLLM_MODEL"]
        assert text == env + (
            "\n# Added by ./setup.sh --update (2026-10-02) from .env.example\n"
            "# Host address each app port is published on.\n"
            "WHATSAPP_API_BIND=\n"
            "\n# New provider key (https://example.com)\n"
            "NEWLLM_API_KEY=\n"
            "\n# Its model\n"
            "NEWLLM_MODEL=fast-1  # default model\n"
        )

    def test_existing_and_commented_keys_are_left_alone(self):
        env = "SERVICE_NAME=\nAI_API_BIND=\nWHATSAPP_API_BIND=\nNEWLLM_API_KEY=\nNEWLLM_MODEL=x\n"
        assert ic.add_missing(env, TEMPLATE) == (env, [])

    def test_values_are_quoted_and_crlf_is_kept(self):
        env = "SERVICE_NAME=mybot\r\nAI_API_BIND=\r\nWHATSAPP_API_BIND=\r\nNEWLLM_MODEL=x\r\n"
        text, added = ic.add_missing(env, TEMPLATE, {"NEWLLM_API_KEY": "s3c$ret"})
        assert added == ["NEWLLM_API_KEY"]
        assert text.startswith(env)
        assert text.endswith("NEWLLM_API_KEY='s3c$ret'\r\n") and "\n" not in text.replace(
            "\r\n", ""
        )
        assert ic.parse_env(text)["NEWLLM_API_KEY"] == "s3c$ret"

    def test_hints(self):
        hints = ic.template_hints(TEMPLATE)
        assert hints["NEWLLM_API_KEY"] == "New provider key (https://example.com)"
        assert hints["WHATSAPP_API_BIND"] == "Host address each app port is published on."


class TestCmdUpdate:
    @pytest.fixture
    def old(self, project):
        (project / ".env.example").write_text(TEMPLATE)
        original = b"SERVICE_NAME=mybot\r\nAI_API_BIND=127.0.0.1\r\nGONE=1\r\n"
        (project / ".env").write_bytes(original)
        return original

    def test_yes_adds_template_values_and_keeps_existing_bytes(self, project, old, capsys):
        assert _main(project, "update", "--yes") == ic.EXIT_OK
        data = (project / ".env").read_bytes()
        assert data.startswith(old)
        env = ic.parse_env(data.decode())
        assert env["NEWLLM_API_KEY"] == "" and env["NEWLLM_MODEL"] == "fast-1"
        backups = list(project.glob(".env.bak.*"))
        assert len(backups) == 1 and backups[0].read_bytes() == old
        out = capsys.readouterr().out
        assert "possibly obsolete, left as is): GONE" in out
        assert _main(project, "update", "--yes") == ic.EXIT_OK
        assert "is up to date" in capsys.readouterr().out

    def test_prompts_only_for_empty_template_values(self, project, old, monkeypatch):
        asked = []
        monkeypatch.setattr(ic.getpass, "getpass", lambda p: asked.append(p) or "k-123")
        answers = iter(["", "y"])  # WHATSAPP_API_BIND left empty, then confirm
        monkeypatch.setattr("builtins.input", lambda p: asked.append(p) or next(answers))
        assert _main(project, "update") == ic.EXIT_OK
        assert [p.split()[0] for p in asked] == ["WHATSAPP_API_BIND", "NEWLLM_API_KEY", "Add"]
        env = ic.parse_env((project / ".env").read_text())
        assert env["NEWLLM_API_KEY"] == "k-123" and env["WHATSAPP_API_BIND"] == ""

    def test_confirmed_asks_values_but_not_the_final_question(self, project, old, monkeypatch):
        asked = []
        monkeypatch.setattr(ic.getpass, "getpass", lambda p: asked.append(p) or "")
        monkeypatch.setattr("builtins.input", lambda p: asked.append(p) or "")
        assert _main(project, "update", "--confirmed") == ic.EXIT_OK
        assert not any(p.startswith("Add") for p in asked) and len(asked) == 2
        assert "NEWLLM_MODEL=fast-1" in (project / ".env").read_text()

    def test_declining_changes_nothing(self, project, old, monkeypatch):
        monkeypatch.setattr(ic.getpass, "getpass", lambda p: "")
        monkeypatch.setattr("builtins.input", lambda p: "n" if p.startswith("Add") else "")
        assert _main(project, "update") == ic.EXIT_CLASH
        assert (project / ".env").read_bytes() == old
        assert not list(project.glob(".env.bak.*"))

    def test_list_and_missing_env(self, project, old, capsys):
        assert _main(project, "update", "--list") == ic.EXIT_OK
        assert capsys.readouterr().out.split() == [
            "WHATSAPP_API_BIND",
            "NEWLLM_API_KEY",
            "NEWLLM_MODEL",
        ]
        (project / ".env").unlink()
        assert _main(project, "update", "--yes") == ic.EXIT_INCOMPLETE
