#!/usr/bin/env python3
"""Cloudflare Registrar CLI for Lana KI.

Secret-safe, audit-first, least-privilege access to the Cloudflare Registrar
API. Standard library only.

Design rules (enforced in code, not only in documentation):

- Read-only by default. Read operations use a dedicated read token.
- Write operations (domain registration, registrar configuration changes) are
  separately gated and never run automatically:
    1. The operator must set ``CF_REGISTRAR_WRITE_ENABLED=true``.
    2. A separate write token ``CF_REGISTRAR_WRITE_TOKEN`` must be present.
    3. A human must confirm on an interactive terminal (TTY). Agents running
       without a TTY are always blocked.
    4. Registration additionally requires ``--confirm`` with the exact phrase
       produced by ``quote`` (domain, years, currency, first-year price and
       renewal price). The quote is re-checked right before submission; any
       price change invalidates the confirmation.
- Idempotency: the domain name is the unique registration key. A local state
  record is claimed atomically before submission. Existing, in-flight,
  unknown or failed records block a new submission; failed records require an
  explicit ``--retry-after-failure`` decision.
- Every operation is written to a JSONL audit log (operation, timestamp,
  status, failure reason). Tokens are never accepted as CLI arguments, never
  printed and are redacted from every logged or printed error message.

Cloudflare domain registrations are non-refundable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Tuple


API_BASE = "https://api.cloudflare.com/client/v4"
HTTP_TIMEOUT_SECONDS = 30
MAX_CHECK_DOMAINS = 20
MAX_REGISTRATION_YEARS = 10
MAX_REASON_LENGTH = 500

ENV_ACCOUNT_ID = "CF_ACCOUNT_ID"
# Environment variable names only; token values are never stored in code.
ENV_READ_TOKEN = "CF_REGISTRAR_READ_TOKEN"  # nosec B105
ENV_WRITE_TOKEN = "CF_REGISTRAR_WRITE_TOKEN"  # nosec B105
ENV_WRITE_ENABLED = "CF_REGISTRAR_WRITE_ENABLED"
ENV_STATE_DIR = "CF_REGISTRAR_STATE_DIR"
ENV_AUDIT_LOG = "CF_REGISTRAR_AUDIT_LOG"

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "cf-registrar"

MODE_READ = "read"
MODE_WRITE = "write"

READ_OPERATIONS = frozenset(
    {
        "search",
        "check",
        "transfer-check",
        "quote",
        "list-domains",
        "get-domain",
        "list-registrations",
        "get-registration",
        "registration-status",
        "update-status",
        "list-extensions",
        "get-extension",
    }
)
WRITE_OPERATIONS = frozenset({"register", "update-domain"})

STATUS_STARTED = "started"
STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"
STATUS_BLOCKED = "blocked"

# Local idempotency states. "submitted" and "unknown" mean the outcome is not
# known and the remote workflow status must be checked before anything else.
STATE_SUBMITTED = "submitted"
STATE_UNKNOWN = "unknown"
STATE_FAILED = "failed"
STATE_SUCCEEDED = "succeeded"

# How long a state update waits for a concurrent holder of the per-domain lock.
LOCK_WAIT_SECONDS = 5.0

# Remote workflow states documented for registration-status / update-status.
REMOTE_WORKFLOW_STATES = frozenset({"pending", "in_progress", "action_required", "blocked", "succeeded", "failed"})

IRREVERSIBLE_WARNING = (
    "WARNING: Domain registration is an irreversible purchase. Cloudflare "
    "Registrar does not refund domain registrations. The account's default "
    "payment method is charged immediately."
)

REDACTED = "[REDACTED]"
AUTH_SCHEME = "Bearer"

_TRUE_VALUES = frozenset({"1", "true", "yes"})
_ACCOUNT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_DOMAIN_RE = re.compile(rf"^(?:{_LABEL}\.)+(?!-)[a-z0-9-]{{2,63}}(?<!-)$")
_EXTENSION_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_=+/.-]{1,512}$")
_BEARER_RE = re.compile(r"(?i)bearer\s+[^\s\"']+")
_TOKEN_LIKE_RE = re.compile(r"[A-Za-z0-9_-]{40,}")

Transport = Callable[[str, str, Mapping[str, str], Optional[bytes], int], Tuple[int, bytes]]
ConfirmFn = Callable[[str], str]


class RegistrarError(Exception):
    """Base error. ``status`` is the audit status recorded for the failure."""

    status = STATUS_FAILURE

    def __init__(self, reason: str, http_status: Optional[int] = None):
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


class GateError(RegistrarError):
    """A safety gate blocked the operation."""

    status = STATUS_BLOCKED


class IdempotencyError(GateError):
    """The domain already has a registration record; no new submission."""


class ConfigError(RegistrarError):
    """Missing or invalid configuration or input."""


class ApiError(RegistrarError):
    """Cloudflare returned an error or the request did not complete.

    ``definitive`` is False when the outcome is unknown (network error, 5xx).
    """

    def __init__(self, reason: str, http_status: Optional[int] = None, definitive: bool = True):
        super().__init__(reason, http_status)
        self.definitive = definitive


# --------------------------------------------------------------------------
# Redaction and audit
# --------------------------------------------------------------------------


def redact(text: Any, secrets: Iterable[str] = ()) -> str:
    """Remove secrets and token-like material from free text."""
    value = str(text)
    for secret in secrets:
        if secret:
            value = value.replace(secret, REDACTED)
    value = _BEARER_RE.sub(AUTH_SCHEME + " " + REDACTED, value)
    value = _TOKEN_LIKE_RE.sub(REDACTED, value)
    return value[:MAX_REASON_LENGTH]


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AuditLog:
    """Append-only JSONL audit log. Entries never contain token material."""

    def __init__(self, path: Path, secrets: Iterable[str] = ()):
        self.path = Path(path)
        self._secrets = tuple(s for s in secrets if s)

    def record(
        self,
        operation: str,
        status: str,
        *,
        mode: str,
        failure_reason: Optional[str] = None,
        domain: Optional[str] = None,
        http_status: Optional[int] = None,
    ) -> dict:
        entry = {
            "timestamp": utc_timestamp(),
            "operation": operation,
            "mode": mode,
            "status": status,
            "failure_reason": redact(failure_reason, self._secrets) if failure_reason else None,
            "domain": domain,
            "http_status": http_status,
        }
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def normalize_domain(name: str) -> str:
    """Return the lowercase punycode FQDN or raise ConfigError."""
    candidate = (name or "").strip().rstrip(".").lower()
    try:
        ascii_name = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ConfigError(f"invalid domain name: {name!r}") from exc
    if len(ascii_name) > 253 or not _DOMAIN_RE.match(ascii_name):
        raise ConfigError(f"invalid domain name: {name!r}")
    return ascii_name


def normalize_extension(name: str) -> str:
    candidate = (name or "").strip().lstrip(".").lower()
    if not _EXTENSION_RE.match(candidate):
        raise ConfigError(f"invalid extension: {name!r}")
    return candidate


def env_flag(env: Mapping[str, str], key: str) -> bool:
    return env.get(key, "").strip().lower() in _TRUE_VALUES


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------


def urllib_transport(
    method: str, url: str, headers: Mapping[str, str], data: Optional[bytes], timeout: int
) -> Tuple[int, bytes]:
    if not url.startswith(API_BASE + "/"):
        raise ApiError("refusing request outside the Cloudflare API base URL")
    request = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
    try:
        # The URL is constrained to the fixed https Cloudflare API base above.
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310  # nosemgrep
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class RegistrarClient:
    """Thin Cloudflare Registrar API client bound to one account and one token."""

    def __init__(self, account_id: str, token: str, transport: Optional[Transport] = None):
        if not _ACCOUNT_ID_RE.match(account_id or ""):
            raise ConfigError(f"{ENV_ACCOUNT_ID} must be a 32-character hex Cloudflare account ID")
        if not token:
            raise ConfigError("API token is not configured")
        self.account_id = account_id
        self._token = token
        self._transport = transport or urllib_transport

    def __repr__(self) -> str:
        return f"RegistrarClient(account_id={self.account_id!r}, token={REDACTED!r})"

    def _url(self, path: str, query: Optional[Mapping[str, Any]] = None) -> str:
        url = f"{API_BASE}/accounts/{self.account_id}/registrar/{path}"
        if query:
            clean = {k: v for k, v in query.items() if v not in (None, [], "")}
            if clean:
                url += "?" + urllib.parse.urlencode(clean, doseq=True)
        return url

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        headers = {
            "Authorization": AUTH_SCHEME + " " + self._token,
            "Accept": "application/json",
            "User-Agent": "lana-ki-cf-registrar/0.1.0",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        if extra_headers:
            headers.update(extra_headers)
        try:
            status, raw = self._transport(method, self._url(path, query), headers, data, HTTP_TIMEOUT_SECONDS)
        except ApiError:
            raise
        except (urllib.error.URLError, OSError) as exc:
            raise ApiError(redact(f"network error: {exc}", (self._token,)), definitive=False) from None
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        if status >= 400 or (isinstance(payload, dict) and payload.get("success") is False):
            messages = []
            if isinstance(payload, dict):
                for err in payload.get("errors") or []:
                    if isinstance(err, dict):
                        messages.append(f"{err.get('code', '')}: {err.get('message', '')}".strip(": "))
            reason = "; ".join(messages) or f"HTTP {status}"
            raise ApiError(redact(reason, (self._token,)), http_status=status, definitive=status < 500)
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload


# --------------------------------------------------------------------------
# Read-only operations
# --------------------------------------------------------------------------


def op_search(client: RegistrarClient, query: str, extensions: Iterable[str] = (), limit: Optional[int] = None) -> Any:
    if not query or not query.strip():
        raise ConfigError("search query must not be empty")
    if limit is not None and limit < 1:
        raise ConfigError("limit must be at least 1")
    return client.request(
        "GET",
        "domain-search",
        query={"q": query.strip(), "extensions": [normalize_extension(e) for e in extensions], "limit": limit},
    )


def _domain_batch(domains: Iterable[str]) -> list:
    names = [normalize_domain(d) for d in domains]
    if not names:
        raise ConfigError("at least one domain is required")
    if len(names) > MAX_CHECK_DOMAINS:
        raise ConfigError(f"at most {MAX_CHECK_DOMAINS} domains per request")
    return names


def op_check(client: RegistrarClient, domains: Iterable[str]) -> Any:
    return client.request("POST", "domain-check", body={"domains": _domain_batch(domains)})


def op_transfer_check(client: RegistrarClient, domains: Iterable[str]) -> Any:
    # Auth/EPP codes are secrets and are intentionally not accepted here.
    names = _domain_batch(domains)
    return client.request("POST", "domain-transfer-check", body={"domains": [{"domain_name": n} for n in names]})


def op_list_domains(client: RegistrarClient) -> Any:
    return client.request("GET", "domains")


def op_get_domain(client: RegistrarClient, domain: str) -> Any:
    return client.request("GET", f"domains/{normalize_domain(domain)}")


def op_list_registrations(client: RegistrarClient, cursor: Optional[str] = None) -> Any:
    if cursor is not None and not _CURSOR_RE.match(cursor):
        raise ConfigError("invalid pagination cursor")
    return client.request("GET", "registrations", query={"cursor": cursor})


def op_get_registration(client: RegistrarClient, domain: str) -> Any:
    return client.request("GET", f"registrations/{normalize_domain(domain)}")


def op_registration_status(client: RegistrarClient, domain: str) -> Any:
    return client.request("GET", f"registrations/{normalize_domain(domain)}/registration-status")


def op_update_status(client: RegistrarClient, domain: str) -> Any:
    return client.request("GET", f"registrations/{normalize_domain(domain)}/update-status")


def op_list_extensions(client: RegistrarClient) -> Any:
    return client.request("GET", "extensions")


def op_get_extension(client: RegistrarClient, extension: str) -> Any:
    return client.request("GET", f"extensions/{urllib.parse.quote(normalize_extension(extension))}")


def _decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ApiError(f"unexpected {field} value in pricing response") from exc


def confirmation_phrase(quote: Mapping[str, Any]) -> str:
    return (
        f"REGISTER {quote['domain']} YEARS={quote['years']} "
        f"{quote['currency']} FIRST_YEAR={quote['first_year_price']} "
        f"RENEWAL={quote['renewal_price']}"
    )


def op_quote(client: RegistrarClient, domain: str, years: int = 1) -> dict:
    """Read-only: real-time availability + pricing, and the purchase gate summary."""
    name = normalize_domain(domain)
    if not 1 <= years <= MAX_REGISTRATION_YEARS:
        raise ConfigError(f"years must be between 1 and {MAX_REGISTRATION_YEARS}")
    result = op_check(client, [name])
    entries = result.get("domains") if isinstance(result, dict) else None
    match = next((d for d in entries or [] if isinstance(d, dict) and d.get("name") == name), None)
    if match is None:
        raise ApiError(f"no availability result returned for {name}")
    if not match.get("registrable"):
        raise GateError(f"{name} is not registrable via API: {match.get('reason') or 'unknown reason'}")
    if match.get("tier") == "premium":
        raise GateError(f"{name} is a premium domain; premium registration must be done manually")
    pricing = match.get("pricing") or {}
    currency = str(pricing.get("currency") or "")
    first_year = pricing.get("registration_cost")
    renewal = pricing.get("renewal_cost")
    if not currency or first_year is None or renewal is None:
        raise ApiError(f"incomplete pricing returned for {name}")
    total = _decimal(first_year, "registration_cost") + _decimal(renewal, "renewal_cost") * (years - 1)
    quote = {
        "domain": name,
        "years": years,
        "currency": currency,
        "first_year_price": str(first_year),
        "renewal_price": str(renewal),
        "estimated_total_for_term": str(total),
        "tier": match.get("tier") or "standard",
        "auto_renew": False,
        "warning": IRREVERSIBLE_WARNING,
    }
    quote["confirmation_phrase"] = confirmation_phrase(quote)
    return quote


# --------------------------------------------------------------------------
# Idempotency state
# --------------------------------------------------------------------------


class StateStore:
    """One JSON file per domain. The domain name is the idempotency key."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path(self, domain: str) -> Path:
        return self.directory / f"{normalize_domain(domain)}.json"

    def get(self, domain: str) -> Optional[dict]:
        path = self._path(domain)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt record must never be treated as "no record".
            return {"domain": normalize_domain(domain), "state": STATE_UNKNOWN, "corrupt": True}

    @staticmethod
    def _write(path: Path, record: Mapping[str, Any], flags: int) -> None:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)

    def _acquire_lock(self, name: str, wait_seconds: float = 0.0) -> Path:
        """Take the per-domain O_EXCL lock that guards every state-file write."""
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = self.directory / f"{name}.lock"
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                os.close(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
                return lock
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise IdempotencyError(
                        f"{name}: another operation holds the lock ({lock.name}); if no process is running, "
                        "verify registration-status before removing the stale lock manually"
                    ) from None
                time.sleep(0.05)  # nosemgrep: python.lang.best-practice.sleep.arbitrary-sleep -- bounded lock back-off

    def claim(self, domain: str, record: Mapping[str, Any], replace_failed: bool = False) -> None:
        """Atomically create the record; fail if one already exists.

        The whole check-and-create runs under an O_EXCL lock file, so concurrent
        submissions (including concurrent ``--retry-after-failure``) cannot both
        proceed. A failed record is archived (never deleted) before replacement.
        """
        name = normalize_domain(domain)
        path = self._path(name)
        lock = self._acquire_lock(name)
        try:
            record = dict(record)
            if replace_failed:
                existing = self.get(name)
                if existing is not None:
                    if existing.get("state") != STATE_FAILED:
                        raise IdempotencyError(f"{name}: --retry-after-failure only applies to a failed record")
                    archive_dir = self.directory / "archive"
                    archive_dir.mkdir(mode=0o700, exist_ok=True)
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                    archived = archive_dir / f"{name}.{stamp}.json"
                    try:
                        os.rename(path, archived)
                    except FileNotFoundError:
                        raise IdempotencyError(
                            f"{name}: the failed record disappeared before it could be archived; review and retry"
                        ) from None
                    record["previous_failure"] = {
                        "archived_record": f"archive/{archived.name}",
                        "failure_reason": existing.get("failure_reason"),
                    }
            try:
                self._write(path, record, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                raise IdempotencyError(f"{name}: a registration record already exists") from None
        finally:
            lock.unlink()

    def update(self, domain: str, only_if_absent: bool = False, **fields: Any) -> Optional[dict]:
        """Read-modify-write under the per-domain lock.

        With ``only_if_absent`` an existing record is left untouched (and
        returned unchanged), so a readback never overwrites a submission or a
        recorded failure reason.
        """
        name = normalize_domain(domain)
        lock = self._acquire_lock(name, wait_seconds=LOCK_WAIT_SECONDS)
        try:
            existing = self.get(name)
            if only_if_absent and existing is not None:
                return existing
            record = existing or {"domain": name}
            record.pop("corrupt", None)
            record.update(fields)
            record["updated_at"] = utc_timestamp()
            path = self._path(name)
            tmp = path.with_suffix(".json.tmp")
            self._write(tmp, record, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            os.replace(tmp, path)
            return record
        finally:
            lock.unlink()


def check_local_idempotency(store: StateStore, domain: str, retry_after_failure: bool) -> None:
    record = store.get(domain)
    if record is None:
        return
    state = record.get("state")
    if state == STATE_FAILED:
        if retry_after_failure:
            return
        raise IdempotencyError(
            f"{domain}: previous registration failed ({record.get('failure_reason') or 'no reason recorded'}). "
            "Review the failure; retry only with an explicit --retry-after-failure decision."
        )
    if state == STATE_SUCCEEDED:
        raise IdempotencyError(f"{domain}: already registered according to local state")
    raise IdempotencyError(
        f"{domain}: a registration is already {state or 'recorded'}; "
        "run 'registration-status' instead of submitting again"
    )


# --------------------------------------------------------------------------
# Write operations (gated)
# --------------------------------------------------------------------------


def require_interactive_terminal() -> None:
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise GateError(
            "write operations require a human at an interactive terminal; "
            "run this command manually, outside the agent"
        )


def tty_confirm(prompt: str) -> str:
    """Default human confirmation. Blocks when no interactive terminal exists."""
    require_interactive_terminal()
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return sys.stdin.readline().strip()


def require_write_gate(env: Mapping[str, str]) -> None:
    if not env_flag(env, ENV_WRITE_ENABLED):
        raise GateError(f"write operations are disabled (read-only default); set {ENV_WRITE_ENABLED}=true to enable")
    if not env.get(ENV_WRITE_TOKEN):
        raise GateError(f"{ENV_WRITE_TOKEN} is not configured; writes need a separate least-privilege token")


def human_confirm(confirm_fn: ConfirmFn, domain: str, summary_lines: Iterable[str]) -> None:
    prompt = "\n".join(list(summary_lines) + [f"Type the domain name '{domain}' to confirm: "])
    answer = confirm_fn(prompt)
    if (answer or "").strip().lower().rstrip(".") != domain:
        raise GateError("human confirmation did not match; nothing was submitted")


def remote_registration_exists(client: RegistrarClient, domain: str) -> bool:
    try:
        op_get_registration(client, domain)
    except ApiError as exc:
        if exc.http_status == 404:
            return False
        raise
    return True


def _state_from_workflow(workflow: Any) -> dict:
    if not isinstance(workflow, dict):
        return {"state": STATE_UNKNOWN}
    remote_state = workflow.get("state")
    fields = {"state": remote_state or STATE_UNKNOWN, "remote_state": remote_state}
    if remote_state == "failed":
        fields["state"] = STATE_FAILED
        error = workflow.get("error") or {}
        if isinstance(error, dict):
            fields["failure_reason"] = f"{error.get('code', '')}: {error.get('message', '')}".strip(": ")
    elif remote_state == "succeeded":
        fields["state"] = STATE_SUCCEEDED
    return fields


def op_register(
    client: RegistrarClient,
    store: StateStore,
    audit: AuditLog,
    confirm_fn: ConfirmFn,
    domain: str,
    years: int,
    confirm: str,
    retry_after_failure: bool = False,
) -> dict:
    name = normalize_domain(domain)
    check_local_idempotency(store, name, retry_after_failure)
    if remote_registration_exists(client, name):
        store.update(name, only_if_absent=True, state=STATE_SUCCEEDED, source="remote-readback")
        raise IdempotencyError(f"{name}: a registration already exists in the Cloudflare account")

    quote = op_quote(client, name, years)
    if (confirm or "").strip() != quote["confirmation_phrase"]:
        raise GateError(
            "confirmation phrase does not match the current quote (domain, years, currency or price changed); "
            "run 'quote' again and show the new summary to the user"
        )
    human_confirm(
        confirm_fn,
        name,
        [
            "=== Cloudflare Registrar purchase gate ===",
            f"Domain:            {quote['domain']}",
            f"Years:             {quote['years']}",
            f"Currency:          {quote['currency']}",
            f"First-year price:  {quote['first_year_price']}",
            f"Renewal price:     {quote['renewal_price']} per year",
            f"Estimated total:   {quote['estimated_total_for_term']}",
            "Auto-renew:        off",
            quote["warning"],
        ],
    )

    # Write-ahead claim: from here on the domain is locked against resubmission.
    store.claim(
        name,
        {
            "domain": name,
            "state": STATE_SUBMITTED,
            "submitted_at": utc_timestamp(),
            "quote": {k: quote[k] for k in ("years", "currency", "first_year_price", "renewal_price")},
        },
        replace_failed=retry_after_failure,
    )
    audit.record("register.submit", STATUS_STARTED, mode=MODE_WRITE, domain=name)
    body = {"domain_name": name, "years": years, "auto_renew": False, "privacy_mode": "redaction"}
    try:
        workflow = client.request("POST", "registrations", body=body, extra_headers={"Prefer": "respond-async"})
    except ApiError as exc:
        # Definitive 4xx: failed (human review before any retry).
        # Network error / 5xx: outcome unknown; never retried automatically.
        state = STATE_FAILED if exc.definitive else STATE_UNKNOWN
        try:
            store.update(name, state=state, failure_reason=exc.reason, http_status=exc.http_status)
        except (OSError, IdempotencyError) as state_exc:
            # The local record stays "submitted", which still blocks resubmission.
            raise RegistrarError(
                f"{exc.reason}; additionally the local state could not be recorded "
                f"({state_exc.__class__.__name__}). Do not resubmit, run 'registration-status'"
            ) from None
        raise
    try:
        store.update(name, **_state_from_workflow(workflow))
    except (OSError, IdempotencyError) as exc:
        # The local record stays "submitted", which keeps the domain locked.
        raise RegistrarError(
            f"registration was submitted but the local state could not be updated ({exc.__class__.__name__}); "
            "do not resubmit, run 'registration-status'"
        ) from None
    return {"domain": name, "workflow": workflow}


def op_status(client: RegistrarClient, store: StateStore, domain: str) -> dict:
    """Read-only: fetch the registration workflow status and sync local state."""
    name = normalize_domain(domain)
    workflow = op_registration_status(client, name)
    record = store.get(name)
    remote_state = workflow.get("state") if isinstance(workflow, dict) else None
    # Only sync a recognized remote state, and never move a terminal local
    # "succeeded" record (or overwrite a recorded failure) to anything else.
    if (
        record is not None
        and remote_state in REMOTE_WORKFLOW_STATES
        and record.get("state") != STATE_SUCCEEDED
        and not (record.get("state") == STATE_FAILED and remote_state != "succeeded")
    ):
        record = store.update(name, **_state_from_workflow(workflow))
    return {"domain": name, "workflow": workflow, "local_state": record}


def op_update_domain(
    client: RegistrarClient,
    confirm_fn: ConfirmFn,
    domain: str,
    auto_renew: Optional[bool],
    locked: Optional[bool],
    privacy: Optional[bool],
) -> Any:
    name = normalize_domain(domain)
    changes = {k: v for k, v in (("auto_renew", auto_renew), ("locked", locked), ("privacy", privacy)) if v is not None}
    if not changes:
        raise ConfigError("no changes requested; pass --auto-renew, --locked or --privacy")
    lines = ["=== Cloudflare Registrar configuration change ===", f"Domain: {name}"]
    lines += [f"{k}: {str(v).lower()}" for k, v in sorted(changes.items())]
    if changes.get("auto_renew"):
        lines.append("NOTE: auto_renew=true authorizes Cloudflare to charge renewals automatically.")
    if changes.get("locked") is False:
        lines.append("NOTE: unlocking a domain allows it to be transferred away.")
    human_confirm(confirm_fn, name, lines)
    return client.request("PUT", f"domains/{name}", body=changes)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _bool_arg(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("true", "on", "yes", "1"):
        return True
    if lowered in ("false", "off", "no", "0"):
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cf_registrar.py",
        description="Cloudflare Registrar for Lana KI (read-only by default; writes are gated).",
    )
    sub = parser.add_subparsers(dest="operation", required=True)

    p = sub.add_parser("search", help="domain suggestions (non-authoritative)")
    p.add_argument("query")
    p.add_argument("--extension", action="append", default=[], dest="extensions")
    p.add_argument("--limit", type=int)

    p = sub.add_parser("check", help="real-time availability and pricing (max 20)")
    p.add_argument("domains", nargs="+")
    p = sub.add_parser("transfer-check", help="transfer eligibility (no auth codes)")
    p.add_argument("domains", nargs="+")

    p = sub.add_parser("quote", help="purchase gate summary for one domain (read-only)")
    p.add_argument("domain")
    p.add_argument("--years", type=int, default=1)

    sub.add_parser("list-domains", help="list registrar domains")
    sub.add_parser("list-extensions", help="list supported extensions")
    p = sub.add_parser("list-registrations", help="list registrations")
    p.add_argument("--cursor")

    for name in ("get-domain", "get-registration", "registration-status", "update-status"):
        p = sub.add_parser(name)
        p.add_argument("domain")
    p = sub.add_parser("get-extension")
    p.add_argument("extension")

    p = sub.add_parser("register", help="WRITE (gated): register a domain - non-refundable")
    p.add_argument("domain")
    p.add_argument("--years", type=int, default=1)
    p.add_argument("--confirm", required=True, help="exact confirmation_phrase from 'quote'")
    p.add_argument("--retry-after-failure", action="store_true")

    p = sub.add_parser("update-domain", help="WRITE (gated): change auto-renew, lock or privacy")
    p.add_argument("domain")
    p.add_argument("--auto-renew", type=_bool_arg)
    p.add_argument("--locked", type=_bool_arg)
    p.add_argument("--privacy", type=_bool_arg)
    return parser


def _audit_domain(args: argparse.Namespace) -> Optional[str]:
    raw = getattr(args, "domain", None)
    if raw is None:
        return None
    try:
        return normalize_domain(raw)
    except ConfigError:
        return None


def _dispatch(args: argparse.Namespace, client: RegistrarClient, store: StateStore, audit: AuditLog, confirm: ConfirmFn) -> Any:
    op = args.operation
    if op == "search":
        return op_search(client, args.query, args.extensions, args.limit)
    if op == "check":
        return op_check(client, args.domains)
    if op == "transfer-check":
        return op_transfer_check(client, args.domains)
    if op == "quote":
        return op_quote(client, args.domain, args.years)
    if op == "list-domains":
        return op_list_domains(client)
    if op == "get-domain":
        return op_get_domain(client, args.domain)
    if op == "list-registrations":
        return op_list_registrations(client, args.cursor)
    if op == "get-registration":
        return op_get_registration(client, args.domain)
    if op == "registration-status":
        return op_status(client, store, args.domain)
    if op == "update-status":
        return op_update_status(client, args.domain)
    if op == "list-extensions":
        return op_list_extensions(client)
    if op == "get-extension":
        return op_get_extension(client, args.extension)
    if op == "register":
        return op_register(client, store, audit, confirm, args.domain, args.years, args.confirm, args.retry_after_failure)
    if op == "update-domain":
        return op_update_domain(client, confirm, args.domain, args.auto_renew, args.locked, args.privacy)
    raise ConfigError(f"unknown operation {op}")  # pragma: no cover - argparse restricts choices


def main(
    argv: Optional[list] = None,
    env: Optional[Mapping[str, str]] = None,
    transport: Optional[Transport] = None,
    confirm_fn: Optional[ConfirmFn] = None,
    stdout=None,
    stderr=None,
) -> int:
    """CLI entry point.

    ``env``, ``transport`` and ``confirm_fn`` are test hooks only. The console
    entry point never sets them; injecting ``confirm_fn`` replaces the
    interactive-terminal confirmation and must not be used outside tests.
    """
    env = os.environ if env is None else env
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    operation = args.operation
    mode = MODE_WRITE if operation in WRITE_OPERATIONS else MODE_READ

    state_dir = Path(env.get(ENV_STATE_DIR) or DEFAULT_STATE_DIR)
    secrets = (env.get(ENV_READ_TOKEN, ""), env.get(ENV_WRITE_TOKEN, ""))
    audit = AuditLog(Path(env.get(ENV_AUDIT_LOG) or state_dir / "audit.jsonl"), secrets)
    store = StateStore(state_dir)
    domain = _audit_domain(args)

    try:
        if mode == MODE_WRITE:
            require_write_gate(env)
            if confirm_fn is None:
                # Fail fast: without a human at a TTY, no write-token request is made at all.
                require_interactive_terminal()
            # Audit-first: if the audit log cannot be written, nothing is sent.
            audit.record(operation, STATUS_STARTED, mode=mode, domain=domain)
            token = env.get(ENV_WRITE_TOKEN, "")
        else:
            token = env.get(ENV_READ_TOKEN, "")
            if not token:
                raise ConfigError(f"{ENV_READ_TOKEN} is not configured")
        client = RegistrarClient(env.get(ENV_ACCOUNT_ID, ""), token, transport)
        result = _dispatch(args, client, store, audit, confirm_fn or tty_confirm)
    except RegistrarError as exc:
        reason = redact(exc.reason, secrets)
        audit.record(operation, exc.status, mode=mode, failure_reason=reason, domain=domain, http_status=exc.http_status)
        stderr.write(json.dumps({"operation": operation, "status": exc.status, "failure_reason": reason}) + "\n")
        return 2 if exc.status == STATUS_BLOCKED else 1
    except OSError as exc:
        # Local state/filesystem failure. Paths and errno are safe to log; no secrets involved.
        reason = redact(f"local I/O error: {exc.__class__.__name__}: {exc.strerror or exc}", secrets)
        audit.record(operation, STATUS_FAILURE, mode=mode, failure_reason=reason, domain=domain)
        stderr.write(json.dumps({"operation": operation, "status": STATUS_FAILURE, "failure_reason": reason}) + "\n")
        return 1

    audit.record(operation, STATUS_SUCCESS, mode=mode, domain=domain)
    stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
