"""Pure logic of scripts/instance_check.py (several bots on one host).

The script is a standalone, stdlib-only file at the repo root, so it is loaded
by path. Fixtures use the shapes `docker inspect` and `docker compose config
--format json` really return.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "instance_check.py"
if not SCRIPT.is_file():
    pytest.skip("scripts/instance_check.py not present", allow_module_level=True)

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


def _containers(*items):
    return ic.parse_containers(list(items))


class TestEnvFile:
    def test_parse_skips_comments_and_keeps_first(self):
        env = ic.parse_env("# A=1\nA=2\nB=\nA=3\n  C=4\n")
        assert env == {"A": "2", "B": ""}

    def test_set_replaces_in_place_and_leaves_other_lines(self):
        text = "X=1\nAI_API_PORT=8000\n# AI_API_PORT=1\nY=a&b|c\n"
        assert ic.set_env(text, "AI_API_PORT", "8002") == (
            "X=1\nAI_API_PORT=8002\n# AI_API_PORT=1\nY=a&b|c\n"
        )

    def test_set_appends_when_missing(self):
        assert ic.set_env("X=1", "P", "2") == "X=1\nP=2\n"

    def test_set_value_is_literal(self):
        assert ic.set_env("U=old\n", "U", r"a\1b") == "U=a\\1b\n"

    def test_missing_keys_in_template_order(self):
        assert ic.missing_keys("A=1\nB=\nC=3\n", "B=x\n") == ["A", "C"]


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
        text = "container_name: ${SERVICE_NAME:-aiagent}-postgres\n  container_name: ${SERVICE_NAME:-aiagent}-whisper-init\n"
        assert ic.container_suffixes(text) == ["postgres", "whisper-init"]
        assert ic.container_suffixes("container_name: curupira-api") == []


class TestPartition:
    def test_same_project_name_in_another_directory_is_foreign(self):
        containers = _containers(
            _inspect("mine", "bot", OWN, {}),
            _inspect("theirs", "bot", "/srv/other/bot", {}),
            _inspect("plain", "", "", {}),
        )
        own, foreign = ic.partition(containers, OWN + "/")
        assert [c.name for c in own] == ["mine"]
        assert [c.name for c in foreign] == ["theirs", "plain"]


class TestAllocate:
    SPECS = [("A", 5432), ("B", 6379), ("C", 5433)]

    def test_defaults_when_everything_is_free(self):
        assert ic.allocate(self.SPECS, {}, lambda p: True) == (
            {"A": 5432, "B": 6379, "C": 5433},
            [],
        )

    def test_skips_taken_and_never_assigns_twice(self):
        taken = {5432}
        assignments, errors = ic.allocate(self.SPECS, {}, lambda p: p not in taken)
        # A is bumped onto 5433, so C (default 5433) must move on.
        assert assignments == {"A": 5433, "B": 6379, "C": 5434}
        assert errors == []

    def test_keeps_current_port_when_free(self):
        assignments, _ = ic.allocate(self.SPECS, {"A": "5440", "B": "junk"}, lambda p: True)
        assert assignments == {"A": 5440, "B": 6379, "C": 5433}

    def test_current_port_is_reserved_before_others_scan(self):
        # C already sits on 5433; A must not be bumped onto it.
        assignments, _ = ic.allocate(self.SPECS, {"C": "5433"}, lambda p: p != 5432)
        assert assignments == {"A": 5434, "B": 6379, "C": 5433}

    def test_clashing_current_is_rebumped_from_the_default(self):
        assignments, _ = ic.allocate([("A", 8000)], {"A": "8000"}, lambda p: p not in {8000, 8001})
        assert assignments == {"A": 8002}

    def test_exhaustion_is_reported_and_value_kept(self):
        assignments, errors = ic.allocate([("A", 8000)], {"A": "8000"}, lambda p: False, span=3)
        assert errors == ["A"]
        assert assignments == {"A": 8000}


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
    THEIRS = _inspect("other-api", "bot", "/srv/b/bot", {})
    OURS = _inspect("my-api", "bot", OWN, {})

    def _run(self, containers=(), volumes=(), fresh=True, candidate="mybot", explicit=False):
        status, _reason = ic.project_guard(
            "bot", explicit, _containers(*containers), list(volumes), [], OWN, fresh, candidate
        )
        return status

    def test_ok_when_nobody_else_uses_the_name(self):
        assert self._run() == ic.GUARD_OK
        assert (
            self._run(containers=[self.OURS], volumes=[ic.Resource("bot_pg", "bot")]) == ic.GUARD_OK
        )

    def test_ok_when_name_is_set_explicitly(self):
        assert self._run(containers=[self.THEIRS], explicit=True) == ic.GUARD_OK

    def test_offer_only_on_a_first_setup(self):
        assert self._run(containers=[self.THEIRS]) == ic.GUARD_OFFER
        assert self._run(containers=[self.THEIRS], fresh=False) == ic.GUARD_REFUSE

    def test_leftover_volumes_without_containers(self):
        volumes = [ic.Resource("bot_postgres-data", "bot")]
        assert self._run(volumes=volumes) == ic.GUARD_OFFER
        # An existing .env means they may well be OUR volumes (stack is down).
        assert self._run(volumes=volumes, fresh=False) == ic.GUARD_REFUSE

    def test_refuse_when_we_already_have_containers(self):
        assert self._run(containers=[self.THEIRS, self.OURS]) == ic.GUARD_REFUSE

    def test_refuse_when_candidate_is_unusable(self):
        assert self._run(containers=[self.THEIRS], candidate="bot") == ic.GUARD_REFUSE
        assert self._run(containers=[self.THEIRS], candidate="") == ic.GUARD_REFUSE
        taken = [ic.Resource("mybot_pg", "mybot")]
        assert self._run(containers=[self.THEIRS], volumes=taken) == ic.GUARD_REFUSE


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
