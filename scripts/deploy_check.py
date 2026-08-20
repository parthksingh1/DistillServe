"""Probe a deployed DistillServe gateway and print a green/red summary.

Run by ``make deploy-check`` and by the deploy job in CI. Exit code is what
matters to CI: 0 when the deployment is serving, 1 otherwise. A *degraded*
readiness result is reported as a warning but not a failure, because optional
dependencies (trace export) must not block a deploy.

Usage:
    uv run python scripts/deploy_check.py --base-url https://distillserve.onrender.com
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Any

import httpx


def _prepare_stdout() -> bool:
    """Make stdout safe for this summary and report whether colour is usable.

    Two portability problems, handled once: a Windows console defaults to
    cp1252 and raises on the arrow glyph, and ANSI escapes are noise when the
    output is being piped into a log rather than a terminal.
    """
    stream = sys.stdout
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")
    return stream.isatty()


_COLOUR = _prepare_stdout()

GREEN = "\033[32m" if _COLOUR else ""
YELLOW = "\033[33m" if _COLOUR else ""
RED = "\033[31m" if _COLOUR else ""
DIM = "\033[2m" if _COLOUR else ""
RESET = "\033[0m" if _COLOUR else ""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of probing one endpoint."""

    endpoint: str
    ok: bool
    status_code: int | None
    detail: str
    latency_ms: float


async def _probe(client: httpx.AsyncClient, endpoint: str) -> tuple[int, Any, float]:
    """GET ``endpoint`` and return status, parsed body and elapsed milliseconds."""
    response = await client.get(endpoint)
    body: Any
    try:
        body = response.json()
    except ValueError:
        body = None
    return response.status_code, body, response.elapsed.total_seconds() * 1000.0


async def check_liveness(client: httpx.AsyncClient) -> ProbeResult:
    """Assert ``/healthz`` returns 200 and echo the build identity."""
    status, body, elapsed = await _probe(client, "/healthz")
    if status != 200 or not isinstance(body, dict):
        return ProbeResult("/healthz", False, status, f"unexpected response: {body!r}", elapsed)
    identity = body.get("identity", {})
    detail = (
        f"{identity.get('service')} v{identity.get('version')} "
        f"@ {identity.get('git_sha_short')} "
        f"mode={identity.get('mode')} env={identity.get('environment')}"
    )
    return ProbeResult("/healthz", True, status, detail, elapsed)


async def check_readiness(client: httpx.AsyncClient) -> ProbeResult:
    """Assert no *required* dependency is failing.

    503 with only optional failures cannot happen (the gateway returns 200 and
    ``degraded`` for those), so a 503 here is always a real outage.
    """
    status, body, elapsed = await _probe(client, "/readyz")
    if not isinstance(body, dict):
        return ProbeResult("/readyz", False, status, f"unexpected response: {body!r}", elapsed)

    checks = body.get("checks", [])
    failing = [c["name"] for c in checks if c.get("status") != "ok" and c.get("required")]
    degraded = [c["name"] for c in checks if c.get("status") != "ok" and not c.get("required")]
    if failing:
        detail = f"required down: {', '.join(failing)}"
        return ProbeResult("/readyz", False, status, detail, elapsed)
    detail = f"status={body.get('status')}"
    if degraded:
        detail += f" (degraded: {', '.join(degraded)})"
    return ProbeResult("/readyz", True, status, detail, elapsed)


async def check_metrics(client: httpx.AsyncClient) -> ProbeResult:
    """Assert ``/metrics`` scrapes and carries the build-info gauge."""
    response = await client.get("/metrics")
    elapsed = response.elapsed.total_seconds() * 1000.0
    ok = response.status_code == 200 and "distillserve_build_info" in response.text
    detail = "build_info exported" if ok else "build_info gauge missing"
    return ProbeResult("/metrics", ok, response.status_code, detail, elapsed)


async def run_checks(base_url: str, request_timeout: float) -> list[ProbeResult]:
    """Run every probe sequentially against ``base_url``.

    The parameter is `request_timeout`, not `timeout`: it configures httpx's
    per-request deadline rather than a cancellation scope around this call.
    """
    client_timeout = httpx.Timeout(request_timeout)
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=client_timeout) as client:
        results: list[ProbeResult] = []
        for check in (check_liveness, check_readiness, check_metrics):
            try:
                results.append(await check(client))
            except httpx.HTTPError as exc:
                name = check.__name__.removeprefix("check_")
                results.append(ProbeResult(f"/{name}", False, None, str(exc), 0.0))
        return results


def render(base_url: str, results: list[ProbeResult]) -> bool:
    """Print the summary table. Returns True when every probe passed."""
    print(f"\n  DistillServe deploy check → {DIM}{base_url}{RESET}\n")
    for result in results:
        mark = f"{GREEN}PASS{RESET}" if result.ok else f"{RED}FAIL{RESET}"
        code = result.status_code if result.status_code is not None else "---"
        print(
            f"  {mark}  {result.endpoint:<10} {DIM}{code:>4}  "
            f"{result.latency_ms:>7.1f}ms{RESET}  {result.detail}"
        )

    passed = all(result.ok for result in results)
    banner = (
        f"{GREEN}All checks passed — deployment is serving.{RESET}"
        if passed
        else f"{RED}Deploy check failed.{RESET}"
    )
    print(f"\n  {banner}\n")
    return passed


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Base URL of the gateway to probe.",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="Per-request timeout, seconds.")
    args = parser.parse_args(argv)

    results = asyncio.run(run_checks(args.base_url, args.timeout))
    if not render(args.base_url, results):
        print(f"  {YELLOW}Tip:{RESET} check Render logs and the service's env vars.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
