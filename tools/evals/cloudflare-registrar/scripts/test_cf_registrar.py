"""Tests for plugins/cloudflare-registrar/scripts/cf_registrar.py.

Run with pytest: `pytest tools/evals/cloudflare-registrar/scripts/test_cf_registrar.py`

No network access: every test injects a fake transport. All tokens below are
obviously fake placeholders.
"""

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

SCRIPT_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent.parent
    / "plugins" / "cloudflare-registrar" / "scripts"
)
sys.path.insert(0, str(SCRIPT_DIR))

import cf_registrar as cfr  # noqa: E402

ACCOUNT_ID = "0123456789abcdef0123456789abcdef"
READ_TOKEN = "fake-read-" + "r" * 30
WRITE_TOKEN = "fake-write-" + "w" * 30
DOMAIN = "lana-example.dev"
API = f"{cfr.API_BASE}/accounts/{ACCOUNT_ID}/registrar"

CHECK_OK = {
    "domains": [
        {
            "name": DOMAIN,
            "registrable": True,
            "tier": "standard",
            "pricing": {"currency": "USD", "registration_cost": "10.18", "renewal_cost": "12.18"},
        }
    ]
}
PHRASE_1Y = f"REGISTER {DOMAIN} YEARS=1 USD FIRST_YEAR=10.18 RENEWAL=12.18"
WORKFLOW_PENDING = {
    "completed": False,
    "state": "pending",
    "created_at": "2026-10-15T00:00:00Z",
    "updated_at": "2026-10-15T00:00:00Z",
    "links": {"self": "x"},
}


def auth_header(token):
    return cfr.AUTH_SCHEME + " " + token


def envelope(result, success=True, errors=None):
    return json.dumps({"success": success, "errors": errors or [], "messages": [], "result": result}).encode()


class FakeTransport:
    """Routes (method, path-without-query) to (status, body). Records calls."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers), "body": json.loads(data) if data else None}
        )
        path = url[len(API):].split("?", 1)[0]
        handler = self.routes.get((method, path))
        if handler is None:
            return 404, envelope(None, success=False, errors=[{"code": 10000, "message": "not found"}])
        if callable(handler):
            return handler()
        return handler

    def methods(self):
        return [(c["method"], c["url"][len(API):].split("?", 1)[0]) for c in self.calls]


@pytest.fixture
def env(tmp_path):
    return {
        cfr.ENV_ACCOUNT_ID: ACCOUNT_ID,
        cfr.ENV_READ_TOKEN: READ_TOKEN,
        cfr.ENV_STATE_DIR: str(tmp_path / "state"),
        cfr.ENV_AUDIT_LOG: str(tmp_path / "audit.jsonl"),
    }


@pytest.fixture
def write_env(env):
    return {**env, cfr.ENV_WRITE_ENABLED: "true", cfr.ENV_WRITE_TOKEN: WRITE_TOKEN}


def run(argv, env, transport, confirm_fn=None):
    out, err = StringIO(), StringIO()
    code = cfr.main(argv, env=env, transport=transport, _confirm_fn=confirm_fn, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def audit_entries(env):
    path = Path(env[cfr.ENV_AUDIT_LOG])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def registration_routes(check=CHECK_OK, post=(202, envelope(WORKFLOW_PENDING))):
    return {
        ("POST", "/domain-check"): (200, envelope(check)),
        ("POST", "/registrations"): post,
    }


def human_types(answer):
    prompts = []

    def _confirm(prompt):
        prompts.append(prompt)
        return answer

    _confirm.prompts = prompts
    return _confirm


def posted_registrations(transport):
    return [m for m in transport.methods() if m == ("POST", "/registrations")]


# ---------------------------------------------------------------- read-only


@pytest.mark.parametrize(
    "argv, method, path",
    [
        (["search", "lana ki", "--extension", "dev", "--limit", "5"], "GET", "/domain-search"),
        (["check", DOMAIN, "other.app"], "POST", "/domain-check"),
        (["transfer-check", DOMAIN], "POST", "/domain-transfer-check"),
        (["list-domains"], "GET", "/domains"),
        (["get-domain", DOMAIN], "GET", f"/domains/{DOMAIN}"),
        (["list-registrations"], "GET", "/registrations"),
        (["get-registration", DOMAIN], "GET", f"/registrations/{DOMAIN}"),
        (["registration-status", DOMAIN], "GET", f"/registrations/{DOMAIN}/registration-status"),
        (["update-status", DOMAIN], "GET", f"/registrations/{DOMAIN}/update-status"),
        (["list-extensions"], "GET", "/extensions"),
        (["get-extension", "dev"], "GET", "/extensions/dev"),
    ],
)
def test_read_operations_use_read_token_and_expected_endpoint(env, argv, method, path):
    transport = FakeTransport({(method, path): (200, envelope({"ok": True}))})
    code, out, _ = run(argv, env, transport)
    assert code == 0  # nosec B101 - test assertion
    assert transport.methods() == [(method, path)]  # nosec B101 - test assertion
    assert transport.calls[0]["headers"]["Authorization"] == auth_header(READ_TOKEN)  # nosec B101 - test assertion
    assert argv[0] in cfr.READ_OPERATIONS  # nosec B101 - test assertion
    assert READ_TOKEN not in out  # nosec B101 - test assertion


def test_read_and_write_operation_sets_are_separate():
    assert cfr.READ_OPERATIONS.isdisjoint(cfr.WRITE_OPERATIONS)  # nosec B101 - test assertion
    assert cfr.WRITE_OPERATIONS == {"register", "update-domain"}  # nosec B101 - test assertion


def test_read_operations_work_without_write_settings(env):
    assert cfr.ENV_WRITE_ENABLED not in env and cfr.ENV_WRITE_TOKEN not in env  # nosec B101 - test assertion
    transport = FakeTransport({("GET", "/domains"): (200, envelope([]))})
    assert run(["list-domains"], env, transport)[0] == 0  # nosec B101 - test assertion


def test_search_serializes_extensions_as_repeated_params(env):
    transport = FakeTransport({("GET", "/domain-search"): (200, envelope({"domains": []}))})
    run(["search", "lana", "--extension", ".DEV", "--extension", "app"], env, transport)
    assert transport.calls[0]["url"].endswith("?q=lana&extensions=dev&extensions=app")  # nosec B101 - test assertion


def test_check_and_transfer_check_bodies(env):
    transport = FakeTransport(
        {
            ("POST", "/domain-check"): (200, envelope({"domains": []})),
            ("POST", "/domain-transfer-check"): (200, envelope({"domains": []})),
        }
    )
    run(["check", "Lana-Example.DEV."], env, transport)
    run(["transfer-check", DOMAIN], env, transport)
    assert transport.calls[0]["body"] == {"domains": [DOMAIN]}  # nosec B101 - test assertion
    assert transport.calls[1]["body"] == {"domains": [{"domain_name": DOMAIN}]}  # nosec B101 - test assertion


def test_check_rejects_more_than_20_domains(env):
    transport = FakeTransport()
    code, _, err = run(["check"] + [f"d{i}.dev" for i in range(21)], env, transport)
    assert code == 1 and transport.calls == []  # nosec B101 - test assertion
    assert "at most 20" in err  # nosec B101 - test assertion


@pytest.mark.parametrize("bad", ["../etc/passwd", "example", "-bad.com", "a..com", "ex ample.com", "x.com/../y"])
def test_invalid_domains_are_rejected_before_any_request(env, bad):
    transport = FakeTransport()
    code, _, _ = run(["get-domain", "--", bad], env, transport)
    assert code == 1  # nosec B101 - test assertion
    assert transport.calls == []  # nosec B101 - test assertion


def test_idn_domains_are_normalized_to_punycode():
    assert cfr.normalize_domain("Bücher.DE.") == "xn--bcher-kva.de"  # nosec B101 - test assertion


def test_read_requires_read_token_even_if_write_token_present(env):
    env = {**env, cfr.ENV_WRITE_TOKEN: WRITE_TOKEN}
    del env[cfr.ENV_READ_TOKEN]
    transport = FakeTransport()
    code, _, err = run(["list-domains"], env, transport)
    assert code == 1 and transport.calls == []  # nosec B101 - test assertion
    assert cfr.ENV_READ_TOKEN in err  # nosec B101 - test assertion


def test_invalid_account_id_rejected(env):
    env = {**env, cfr.ENV_ACCOUNT_ID: "../../zones"}
    transport = FakeTransport()
    code, _, _ = run(["list-domains"], env, transport)
    assert code == 1 and transport.calls == []  # nosec B101 - test assertion


def test_transport_refuses_foreign_hosts():
    with pytest.raises(cfr.ApiError):
        cfr.urllib_transport("GET", "https://evil.example/client/v4/x", {}, None, 1)


# ---------------------------------------------------------------- quote / purchase gate


def test_quote_surfaces_all_purchase_gate_fields(env):
    transport = FakeTransport({("POST", "/domain-check"): (200, envelope(CHECK_OK))})
    code, out, _ = run(["quote", DOMAIN, "--years", "2"], env, transport)
    assert code == 0  # nosec B101 - test assertion
    quote = json.loads(out)
    assert quote["domain"] == DOMAIN  # nosec B101 - test assertion
    assert quote["currency"] == "USD"  # nosec B101 - test assertion
    assert quote["first_year_price"] == "10.18"  # nosec B101 - test assertion
    assert quote["renewal_price"] == "12.18"  # nosec B101 - test assertion
    assert quote["estimated_total_for_term"] == "22.36"  # nosec B101 - test assertion
    assert "irreversible" in quote["warning"].lower()  # nosec B101 - test assertion
    assert "refund" in quote["warning"].lower()  # nosec B101 - test assertion
    assert quote["confirmation_phrase"] == f"REGISTER {DOMAIN} YEARS=2 USD FIRST_YEAR=10.18 RENEWAL=12.18"  # nosec B101 - test assertion
    assert transport.methods() == [("POST", "/domain-check")]  # nosec B101 - test assertion


@pytest.mark.parametrize(
    "entry",
    [
        {"name": DOMAIN, "registrable": False, "reason": "domain_unavailable"},
        {
            "name": DOMAIN,
            "registrable": True,
            "tier": "premium",
            "pricing": {"currency": "USD", "registration_cost": "999", "renewal_cost": "10"},
        },
    ],
)
def test_quote_blocks_unavailable_and_premium(env, entry):
    transport = FakeTransport({("POST", "/domain-check"): (200, envelope({"domains": [entry]}))})
    code, _, _ = run(["quote", DOMAIN], env, transport)
    assert code == 2  # nosec B101 - test assertion


# ---------------------------------------------------------------- write gating


def test_register_blocked_by_default_read_only(env):
    transport = FakeTransport(registration_routes())
    confirm = human_types(DOMAIN)
    code, _, err = run(["register", DOMAIN, "--confirm", PHRASE_1Y], env, transport, confirm)
    assert code == 2  # nosec B101 - test assertion
    assert transport.calls == []  # nosec B101 - test assertion
    assert confirm.prompts == []  # nosec B101 - test assertion
    assert "read-only default" in err  # nosec B101 - test assertion


def test_register_blocked_without_separate_write_token(env):
    env = {**env, cfr.ENV_WRITE_ENABLED: "true"}
    transport = FakeTransport(registration_routes())
    code, _, _ = run(["register", DOMAIN, "--confirm", PHRASE_1Y], env, transport, human_types(DOMAIN))
    assert code == 2 and transport.calls == []  # nosec B101 - test assertion


def test_register_blocked_without_interactive_terminal(write_env, monkeypatch):
    # The default confirm_fn (tty_confirm) refuses without a TTY, which is
    # always the case for agents.
    monkeypatch.setattr(sys, "stdin", StringIO(DOMAIN + "\n"))
    transport = FakeTransport(registration_routes())
    code, _, err = run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, transport, None)
    assert code == 2  # nosec B101 - test assertion
    assert transport.calls == []  # nosec B101 - test assertion
    assert "interactive terminal" in err  # nosec B101 - test assertion
    code, _, _ = run(["update-domain", DOMAIN, "--locked", "false"], write_env, transport, None)
    assert code == 2  # nosec B101 - test assertion
    assert transport.calls == []  # nosec B101 - test assertion


def test_register_blocked_when_confirmation_phrase_mismatches(write_env):
    transport = FakeTransport(registration_routes())
    confirm = human_types(DOMAIN)
    stale = PHRASE_1Y.replace("10.18", "9.99")
    code, _, _ = run(["register", DOMAIN, "--confirm", stale], write_env, transport, confirm)
    assert code == 2  # nosec B101 - test assertion
    assert posted_registrations(transport) == []  # nosec B101 - test assertion
    assert confirm.prompts == []  # nosec B101 - test assertion


def test_register_blocked_when_human_types_wrong_domain(write_env):
    transport = FakeTransport(registration_routes())
    code, _, _ = run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, transport, human_types("yes"))
    assert code == 2  # nosec B101 - test assertion
    assert posted_registrations(transport) == []  # nosec B101 - test assertion


def test_register_happy_path_shows_gate_and_uses_write_token(write_env):
    transport = FakeTransport(registration_routes())
    confirm = human_types(DOMAIN)
    code, out, _ = run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, transport, confirm)
    assert code == 0  # nosec B101 - test assertion
    prompt = confirm.prompts[0]
    for needle in (DOMAIN, "USD", "10.18", "12.18", "irreversible", "refund"):
        assert needle in prompt  # nosec B101 - test assertion
    post = [c for c in transport.calls if c["method"] == "POST" and c["url"].endswith("/registrations")]
    assert len(post) == 1  # nosec B101 - test assertion
    assert post[0]["headers"]["Authorization"] == auth_header(WRITE_TOKEN)  # nosec B101 - test assertion
    assert post[0]["headers"]["Prefer"] == "respond-async"  # nosec B101 - test assertion
    assert post[0]["body"] == {"domain_name": DOMAIN, "years": 1, "auto_renew": False, "privacy_mode": "redaction"}  # nosec B101 - test assertion
    assert json.loads(out)["workflow"]["state"] == "pending"  # nosec B101 - test assertion


def test_update_domain_is_gated(env, write_env):
    transport = FakeTransport({("PUT", f"/domains/{DOMAIN}"): (200, envelope({}))})
    code, _, _ = run(["update-domain", DOMAIN, "--auto-renew", "true"], env, transport, human_types(DOMAIN))
    assert code == 2 and transport.calls == []  # nosec B101 - test assertion

    code, _, _ = run(["update-domain", DOMAIN, "--locked", "false"], write_env, transport, human_types("no"))
    assert code == 2 and transport.calls == []  # nosec B101 - test assertion

    confirm = human_types(DOMAIN)
    code, _, _ = run(["update-domain", DOMAIN, "--locked", "false"], write_env, transport, confirm)
    assert code == 0  # nosec B101 - test assertion
    assert transport.calls[0]["method"] == "PUT"  # nosec B101 - test assertion
    assert transport.calls[0]["body"] == {"locked": False}  # nosec B101 - test assertion
    assert transport.calls[0]["headers"]["Authorization"] == auth_header(WRITE_TOKEN)  # nosec B101 - test assertion
    assert "transferred away" in confirm.prompts[0]  # nosec B101 - test assertion


# ---------------------------------------------------------------- idempotency


def test_second_register_for_same_domain_is_blocked(write_env):
    transport = FakeTransport(registration_routes())
    assert run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, transport, human_types(DOMAIN))[0] == 0  # nosec B101 - test assertion
    code, _, err = run(["register", DOMAIN.upper(), "--confirm", PHRASE_1Y], write_env, transport, human_types(DOMAIN))
    assert code == 2  # nosec B101 - test assertion
    assert "registration-status" in err  # nosec B101 - test assertion
    assert len(posted_registrations(transport)) == 1  # nosec B101 - test assertion


def test_existing_remote_registration_blocks_submission(write_env):
    routes = registration_routes()
    routes[("GET", f"/registrations/{DOMAIN}")] = (200, envelope({"domain_name": DOMAIN}))
    transport = FakeTransport(routes)
    code, _, _ = run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, transport, human_types(DOMAIN))
    assert code == 2  # nosec B101 - test assertion
    assert posted_registrations(transport) == []  # nosec B101 - test assertion
    store = cfr.StateStore(Path(write_env[cfr.ENV_STATE_DIR]))
    assert store.get(DOMAIN)["state"] == cfr.STATE_SUCCEEDED  # nosec B101 - test assertion


def test_failed_registration_is_not_blindly_retried(write_env):
    failing = registration_routes(
        post=(400, envelope(None, success=False, errors=[{"code": 1, "message": "registry rejected"}]))
    )
    transport = FakeTransport(failing)
    code, _, _ = run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, transport, human_types(DOMAIN))
    assert code == 1  # nosec B101 - test assertion
    store = cfr.StateStore(Path(write_env[cfr.ENV_STATE_DIR]))
    assert store.get(DOMAIN)["state"] == cfr.STATE_FAILED  # nosec B101 - test assertion

    ok = FakeTransport(registration_routes())
    code, _, err = run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, ok, human_types(DOMAIN))
    assert code == 2  # nosec B101 - test assertion
    assert "registry rejected" in err and "--retry-after-failure" in err  # nosec B101 - test assertion
    assert ok.calls == []  # nosec B101 - test assertion

    code, _, _ = run(
        ["register", DOMAIN, "--confirm", PHRASE_1Y, "--retry-after-failure"], write_env, ok, human_types(DOMAIN)
    )
    assert code == 0  # nosec B101 - test assertion
    assert store.get(DOMAIN)["state"] == "pending"  # nosec B101 - test assertion


def test_unknown_outcome_blocks_retry_even_with_retry_flag(write_env):
    transport = FakeTransport(registration_routes(post=(503, b"")))
    code, _, _ = run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, transport, human_types(DOMAIN))
    assert code == 1  # nosec B101 - test assertion
    store = cfr.StateStore(Path(write_env[cfr.ENV_STATE_DIR]))
    assert store.get(DOMAIN)["state"] == cfr.STATE_UNKNOWN  # nosec B101 - test assertion
    retry = FakeTransport(registration_routes())
    code, _, _ = run(
        ["register", DOMAIN, "--confirm", PHRASE_1Y, "--retry-after-failure"], write_env, retry, human_types(DOMAIN)
    )
    assert code == 2  # nosec B101 - test assertion
    assert retry.calls == []  # nosec B101 - test assertion


def test_network_error_marks_unknown(write_env):
    routes = registration_routes()

    def boom():
        raise OSError("connection reset")

    routes[("POST", "/registrations")] = boom
    code, _, _ = run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, FakeTransport(routes), human_types(DOMAIN))
    assert code == 1  # nosec B101 - test assertion
    store = cfr.StateStore(Path(write_env[cfr.ENV_STATE_DIR]))
    assert store.get(DOMAIN)["state"] == cfr.STATE_UNKNOWN  # nosec B101 - test assertion


def test_registration_status_syncs_local_state(write_env):
    run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, FakeTransport(registration_routes()), human_types(DOMAIN))
    done = {**WORKFLOW_PENDING, "completed": True, "state": "succeeded"}
    transport = FakeTransport({("GET", f"/registrations/{DOMAIN}/registration-status"): (200, envelope(done))})
    code, out, _ = run(["registration-status", DOMAIN], write_env, transport)
    assert code == 0  # nosec B101 - test assertion
    assert json.loads(out)["local_state"]["state"] == cfr.STATE_SUCCEEDED  # nosec B101 - test assertion


def test_corrupt_state_record_blocks(write_env):
    state_dir = Path(write_env[cfr.ENV_STATE_DIR])
    state_dir.mkdir(parents=True)
    (state_dir / f"{DOMAIN}.json").write_text("{not json")
    transport = FakeTransport(registration_routes())
    code, _, _ = run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, transport, human_types(DOMAIN))
    assert code == 2 and transport.calls == []  # nosec B101 - test assertion


# ---------------------------------------------------------------- audit log


AUDIT_KEYS = {"timestamp", "operation", "mode", "status", "failure_reason", "domain", "http_status"}


def test_every_operation_is_audited_with_required_fields(env):
    run(["list-domains"], env, FakeTransport({("GET", "/domains"): (200, envelope([]))}))
    run(["get-domain", DOMAIN], env, FakeTransport())  # 404 -> failure
    run(["register", DOMAIN, "--confirm", PHRASE_1Y], env, FakeTransport())  # read-only default -> blocked
    entries = audit_entries(env)
    assert [(e["operation"], e["status"]) for e in entries] == [  # nosec B101 - test assertion
        ("list-domains", "success"),
        ("get-domain", "failure"),
        ("register", "blocked"),
    ]
    for entry in entries:
        assert set(entry) == AUDIT_KEYS  # nosec B101 - test assertion
        assert entry["timestamp"].endswith("Z")  # nosec B101 - test assertion
    assert entries[0]["failure_reason"] is None and entries[0]["mode"] == "read"  # nosec B101 - test assertion
    assert entries[1]["failure_reason"] and entries[1]["http_status"] == 404  # nosec B101 - test assertion
    assert entries[2]["mode"] == "write" and "read-only" in entries[2]["failure_reason"]  # nosec B101 - test assertion


def test_write_operations_are_audited_before_submission(write_env):
    run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, FakeTransport(registration_routes()), human_types(DOMAIN))
    statuses = [(e["operation"], e["status"]) for e in audit_entries(write_env)]
    assert statuses == [("register", "started"), ("register.submit", "started"), ("register", "success")]  # nosec B101 - test assertion


def test_write_fails_closed_if_audit_log_unwritable(write_env, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    env = {**write_env, cfr.ENV_AUDIT_LOG: str(blocker / "audit.jsonl")}
    transport = FakeTransport(registration_routes())
    code, _, err = run(["register", DOMAIN, "--confirm", PHRASE_1Y], env, transport, human_types(DOMAIN))
    assert code == 1  # nosec B101 - test assertion
    assert json.loads(err)["audit_log"].startswith("not writable")  # nosec B101 - test assertion
    assert transport.calls == []  # nosec B101 - test assertion


def test_audit_log_file_is_private(env):
    run(["list-domains"], env, FakeTransport({("GET", "/domains"): (200, envelope([]))}))
    assert (Path(env[cfr.ENV_AUDIT_LOG]).stat().st_mode & 0o077) == 0  # nosec B101 - test assertion


# ---------------------------------------------------------------- secret handling


def test_tokens_never_appear_in_output_audit_log_or_state(env, write_env):
    leaky_error = envelope(
        None, success=False, errors=[{"code": 9, "message": "bad token " + READ_TOKEN + " / " + auth_header(WRITE_TOKEN)}]
    )
    _, out1, err1 = run(["list-domains"], env, FakeTransport({("GET", "/domains"): (403, leaky_error)}))
    routes = registration_routes(post=(400, leaky_error))
    _, out2, err2 = run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, FakeTransport(routes), human_types(DOMAIN))
    state_files = "".join(p.read_text() for p in Path(write_env[cfr.ENV_STATE_DIR]).glob("*.json"))
    combined = out1 + err1 + out2 + err2 + Path(env[cfr.ENV_AUDIT_LOG]).read_text() + state_files
    assert READ_TOKEN not in combined  # nosec B101 - test assertion
    assert WRITE_TOKEN not in combined  # nosec B101 - test assertion
    assert cfr.REDACTED in combined  # nosec B101 - test assertion


def test_redact_handles_auth_headers_and_token_like_strings():
    redacted = cfr.redact("Authorization: " + auth_header("abc.def") + " and " + "Z" * 40)
    assert "abc.def" not in redacted  # nosec B101 - test assertion
    assert "Z" * 40 not in redacted  # nosec B101 - test assertion


def test_client_repr_hides_token():
    client = cfr.RegistrarClient(ACCOUNT_ID, READ_TOKEN, transport=FakeTransport())
    assert READ_TOKEN not in repr(client)  # nosec B101 - test assertion


def test_tokens_cannot_be_passed_on_the_command_line():
    with pytest.raises(SystemExit):
        cfr.build_parser().parse_args(["list-domains", "--token", "x"])


# ---------------------------------------------------------------- review hardening


def test_search_rejects_non_positive_limit(env):
    transport = FakeTransport()
    code, _, _ = run(["search", "lana", "--limit", "0"], env, transport)
    assert code == 1 and transport.calls == []  # nosec B101 - test assertion


def test_concurrent_submission_is_blocked_by_lock(write_env):
    state_dir = Path(write_env[cfr.ENV_STATE_DIR])
    state_dir.mkdir(parents=True)
    (state_dir / f"{DOMAIN}.lock").write_text("")
    transport = FakeTransport(registration_routes())
    code, _, err = run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, transport, human_types(DOMAIN))
    assert code == 2  # nosec B101 - test assertion
    assert "lock" in err  # nosec B101 - test assertion
    assert posted_registrations(transport) == []  # nosec B101 - test assertion


def test_concurrent_retry_after_failure_cannot_both_claim(write_env):
    store = cfr.StateStore(Path(write_env[cfr.ENV_STATE_DIR]))
    store.update(DOMAIN, state=cfr.STATE_FAILED, failure_reason="registry rejected")
    store.claim(DOMAIN, {"domain": DOMAIN, "state": cfr.STATE_SUBMITTED}, replace_failed=True)
    with pytest.raises(cfr.IdempotencyError):
        store.claim(DOMAIN, {"domain": DOMAIN, "state": cfr.STATE_SUBMITTED}, replace_failed=True)
    assert store.get(DOMAIN)["state"] == cfr.STATE_SUBMITTED  # nosec B101 - test assertion


def test_retry_after_failure_archives_previous_record(write_env):
    store = cfr.StateStore(Path(write_env[cfr.ENV_STATE_DIR]))
    store.update(DOMAIN, state=cfr.STATE_FAILED, failure_reason="registry rejected", submitted_at="t0")
    code, _, _ = run(
        ["register", DOMAIN, "--confirm", PHRASE_1Y, "--retry-after-failure"],
        write_env, FakeTransport(registration_routes()), human_types(DOMAIN),
    )
    assert code == 0  # nosec B101 - test assertion
    record = store.get(DOMAIN)
    assert record["previous_failure"]["failure_reason"] == "registry rejected"  # nosec B101 - test assertion
    archived = list((store.directory / "archive").glob(f"{DOMAIN}.*.json"))
    assert len(archived) == 1  # nosec B101 - test assertion
    assert json.loads(archived[0].read_text())["submitted_at"] == "t0"  # nosec B101 - test assertion
    assert not (store.directory / f"{DOMAIN}.lock").exists()  # nosec B101 - test assertion


def test_state_write_failure_after_submit_is_audited_and_keeps_lock(write_env, monkeypatch):
    real_update = cfr.StateStore.update

    def flaky_update(self, domain, **fields):
        if fields.get("state") == "pending":
            raise PermissionError(13, "Permission denied")
        return real_update(self, domain, **fields)

    monkeypatch.setattr(cfr.StateStore, "update", flaky_update)
    transport = FakeTransport(registration_routes())
    code, _, err = run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, transport, human_types(DOMAIN))
    assert code == 1  # nosec B101 - test assertion
    assert "registration-status" in err  # nosec B101 - test assertion
    assert audit_entries(write_env)[-1]["status"] == "failure"  # nosec B101 - test assertion
    store = cfr.StateStore(Path(write_env[cfr.ENV_STATE_DIR]))
    assert store.get(DOMAIN)["state"] == cfr.STATE_SUBMITTED  # nosec B101 - test assertion


def test_local_io_errors_are_audited_not_crashed(write_env, monkeypatch):
    def broken_claim(self, domain, record, replace_failed=False):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cfr.StateStore, "claim", broken_claim)
    transport = FakeTransport(registration_routes())
    code, _, err = run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, transport, human_types(DOMAIN))
    assert code == 1  # nosec B101 - test assertion
    assert "local I/O error" in err  # nosec B101 - test assertion
    assert posted_registrations(transport) == []  # nosec B101 - test assertion
    assert audit_entries(write_env)[-1]["status"] == "failure"  # nosec B101 - test assertion


def test_remote_readback_never_overwrites_existing_local_record(write_env):
    store = cfr.StateStore(Path(write_env[cfr.ENV_STATE_DIR]))
    store.update(DOMAIN, state=cfr.STATE_FAILED, failure_reason="registry rejected")
    routes = registration_routes()
    routes[("GET", f"/registrations/{DOMAIN}")] = (200, envelope({"domain_name": DOMAIN}))
    transport = FakeTransport(routes)
    code, _, _ = run(
        ["register", DOMAIN, "--confirm", PHRASE_1Y, "--retry-after-failure"],
        write_env, transport, human_types(DOMAIN),
    )
    assert code == 2  # nosec B101 - test assertion
    assert posted_registrations(transport) == []  # nosec B101 - test assertion
    record = store.get(DOMAIN)
    assert record["state"] == cfr.STATE_FAILED  # nosec B101 - test assertion
    assert record["failure_reason"] == "registry rejected"  # nosec B101 - test assertion


def test_state_updates_respect_the_domain_lock(write_env, monkeypatch):
    monkeypatch.setattr(cfr, "LOCK_WAIT_SECONDS", 0.0)
    store = cfr.StateStore(Path(write_env[cfr.ENV_STATE_DIR]))
    store.claim(DOMAIN, {"domain": DOMAIN, "state": cfr.STATE_SUBMITTED})
    (store.directory / f"{DOMAIN}.lock").write_text("")
    with pytest.raises(cfr.IdempotencyError):
        store.update(DOMAIN, state=cfr.STATE_SUCCEEDED)
    assert store.get(DOMAIN)["state"] == cfr.STATE_SUBMITTED  # nosec B101 - test assertion


def test_retry_fails_cleanly_if_failed_record_vanishes(write_env, monkeypatch):
    store = cfr.StateStore(Path(write_env[cfr.ENV_STATE_DIR]))
    store.update(DOMAIN, state=cfr.STATE_FAILED, failure_reason="registry rejected")

    def vanished(src, dst):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(cfr.os, "rename", vanished)
    with pytest.raises(cfr.IdempotencyError):
        store.claim(DOMAIN, {"domain": DOMAIN, "state": cfr.STATE_SUBMITTED}, replace_failed=True)
    assert not (store.directory / f"{DOMAIN}.lock").exists()  # nosec B101 - test assertion


@pytest.mark.parametrize("remote", [None, "garbage", {"state": "mystery"}, {**WORKFLOW_PENDING, "state": "pending"}])
def test_registration_status_never_downgrades_succeeded(write_env, remote):
    store = cfr.StateStore(Path(write_env[cfr.ENV_STATE_DIR]))
    store.update(DOMAIN, state=cfr.STATE_SUCCEEDED)
    transport = FakeTransport({("GET", f"/registrations/{DOMAIN}/registration-status"): (200, envelope(remote))})
    run(["registration-status", DOMAIN], write_env, transport)
    assert store.get(DOMAIN)["state"] == cfr.STATE_SUCCEEDED  # nosec B101 - test assertion


def test_registration_status_keeps_recorded_failure_reason(write_env):
    store = cfr.StateStore(Path(write_env[cfr.ENV_STATE_DIR]))
    store.update(DOMAIN, state=cfr.STATE_FAILED, failure_reason="registry rejected")
    remote = {**WORKFLOW_PENDING, "state": "failed", "error": {"code": "x", "message": "other"}}
    transport = FakeTransport({("GET", f"/registrations/{DOMAIN}/registration-status"): (200, envelope(remote))})
    run(["registration-status", DOMAIN], write_env, transport)
    record = store.get(DOMAIN)
    assert record["state"] == cfr.STATE_FAILED  # nosec B101 - test assertion
    assert record["failure_reason"] == "registry rejected"  # nosec B101 - test assertion


def test_api_error_with_state_write_failure_is_surfaced(write_env, monkeypatch):
    real_update = cfr.StateStore.update

    def flaky_update(self, domain, **fields):
        if fields.get("state") in (cfr.STATE_FAILED, cfr.STATE_UNKNOWN):
            raise PermissionError(13, "Permission denied")
        return real_update(self, domain, **fields)

    monkeypatch.setattr(cfr.StateStore, "update", flaky_update)
    routes = registration_routes()
    routes[("POST", "/registrations")] = (400, envelope(None, success=False, errors=[{"code": 1, "message": "rejected"}]))
    code, _, err = run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, FakeTransport(routes), human_types(DOMAIN))
    assert code == 1  # nosec B101 - test assertion
    assert "local state could not be recorded" in err  # nosec B101 - test assertion
    assert "registration-status" in err  # nosec B101 - test assertion
    store = cfr.StateStore(Path(write_env[cfr.ENV_STATE_DIR]))
    assert store.get(DOMAIN)["state"] == cfr.STATE_SUBMITTED  # nosec B101 - test assertion


@pytest.mark.parametrize("tier", [None, "", "platinum"])
def test_quote_fails_closed_on_missing_or_unknown_tier(env, tier):
    entry = {**CHECK_OK["domains"][0], "tier": tier}
    transport = FakeTransport({("POST", "/domain-check"): (200, envelope({"domains": [entry]}))})
    code, _, err = run(["quote", DOMAIN], env, transport)
    assert code == 2  # nosec B101 - test assertion
    assert "standard-tier" in err  # nosec B101 - test assertion


def test_registration_status_returns_remote_status_when_lock_is_held(write_env, monkeypatch):
    monkeypatch.setattr(cfr, "LOCK_WAIT_SECONDS", 0.0)
    store = cfr.StateStore(Path(write_env[cfr.ENV_STATE_DIR]))
    store.claim(DOMAIN, {"domain": DOMAIN, "state": cfr.STATE_SUBMITTED})
    (store.directory / f"{DOMAIN}.lock").write_text("")
    done = {**WORKFLOW_PENDING, "completed": True, "state": "succeeded"}
    transport = FakeTransport({("GET", f"/registrations/{DOMAIN}/registration-status"): (200, envelope(done))})
    code, out, _ = run(["registration-status", DOMAIN], write_env, transport)
    assert code == 0  # nosec B101 - test assertion
    result = json.loads(out)
    assert result["workflow"]["state"] == "succeeded"  # nosec B101 - test assertion
    assert result["local_sync"].startswith("skipped")  # nosec B101 - test assertion
    assert store.get(DOMAIN)["state"] == cfr.STATE_SUBMITTED  # nosec B101 - test assertion


def test_unrecognized_remote_workflow_state_is_stored_as_unknown(write_env):
    weird = {**WORKFLOW_PENDING, "state": "mystery"}
    run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env,
        FakeTransport(registration_routes(post=(202, envelope(weird)))), human_types(DOMAIN))
    record = cfr.StateStore(Path(write_env[cfr.ENV_STATE_DIR])).get(DOMAIN)
    assert record["state"] == cfr.STATE_UNKNOWN  # nosec B101 - test assertion
    assert record["remote_state"] == "mystery"  # nosec B101 - test assertion


def test_remote_registration_message_survives_busy_lock(write_env, monkeypatch):
    monkeypatch.setattr(cfr, "LOCK_WAIT_SECONDS", 0.0)
    state_dir = Path(write_env[cfr.ENV_STATE_DIR])
    state_dir.mkdir(parents=True)
    (state_dir / f"{DOMAIN}.lock").write_text("")
    routes = registration_routes()
    routes[("GET", f"/registrations/{DOMAIN}")] = (200, envelope({"domain_name": DOMAIN}))
    code, _, err = run(["register", DOMAIN, "--confirm", PHRASE_1Y], write_env, FakeTransport(routes), human_types(DOMAIN))
    assert code == 2  # nosec B101 - test assertion
    assert "already exists in the Cloudflare account" in err  # nosec B101 - test assertion


@pytest.mark.parametrize("cost", ["NaN", "Infinity", "-1.00", "abc"])
def test_quote_rejects_malformed_prices(env, cost):
    entry = {**CHECK_OK["domains"][0], "pricing": {"currency": "USD", "registration_cost": cost, "renewal_cost": "12.18"}}
    transport = FakeTransport({("POST", "/domain-check"): (200, envelope({"domains": [entry]}))})
    code, out, _ = run(["quote", DOMAIN], env, transport)
    assert code == 1  # nosec B101 - test assertion
    assert out == ""  # nosec B101 - test assertion


def test_update_domain_audits_submit_before_put(write_env):
    transport = FakeTransport({("PUT", f"/domains/{DOMAIN}"): (200, envelope({"name": DOMAIN}))})
    code, _, _ = run(["update-domain", DOMAIN, "--auto-renew", "false"], write_env, transport, human_types(DOMAIN))
    assert code == 0  # nosec B101 - test assertion
    ops = [(e["operation"], e["status"]) for e in audit_entries(write_env)]
    assert ops == [  # nosec B101 - test assertion
        ("update-domain", "started"), ("update-domain.submit", "started"), ("update-domain", "success"),
    ]
