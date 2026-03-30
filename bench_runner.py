#!/usr/bin/env python3
"""
XBOW Benchmark Runner
---------------------
Builds, runs, health-checks, and probes each benchmark one at a time.
Results are written to a structured JSON log file.

Usage:
    # Run all 104 benchmarks
    python3 bench_runner.py --repo /path/to/validation-benchmarks

    # Run specific benchmarks by number
    python3 bench_runner.py --repo /path/to/validation-benchmarks --benchmarks 1,5,78,95

    # Run a range
    python3 bench_runner.py --repo /path/to/validation-benchmarks --benchmarks 1-10

    # Mix of both
    python3 bench_runner.py --repo /path/to/validation-benchmarks --benchmarks 1-5,78,95

    # Optionally set a custom log output path
    python3 bench_runner.py --repo /path/to/validation-benchmarks --log results.json
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path


# ─── Helpers ──────────────────────────────────────────────────────────────────

BENCH_DIR_PATTERN = re.compile(r"^XBEN-(\d{3})-24$")


def benchmark_name(num: int) -> str:
    return f"XBEN-{num:03d}-24"


def parse_benchmark_selection(spec: str) -> list[int]:
    """
    Parse a comma-separated list of numbers and ranges like '1,5,10-20,78'.
    Returns a sorted list of unique ints.
    """
    nums = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            nums.update(range(int(lo), int(hi) + 1))
        else:
            nums.add(int(part))
    return sorted(nums)


def discover_benchmarks(benchmarks_dir: Path) -> list[int]:
    """Return sorted list of benchmark numbers that exist on disk."""
    found = []
    for entry in benchmarks_dir.iterdir():
        m = BENCH_DIR_PATTERN.match(entry.name)
        if m and entry.is_dir():
            found.append(int(m.group(1)))
    return sorted(found)


def run_cmd(cmd: list[str], cwd: Path, timeout: int = 300) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",    # <--- ADD THIS
            errors="replace",    # <--- ADD THIS
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError as e:
        return -1, "", f"Command not found: {e}"


def get_web_ports(bench_dir: Path) -> list[tuple[str, int]]:
    """
    Parse docker-compose.yml and extract host-mapped ports for web-like services.
    Returns list of (service_name, host_port) tuples.
    Only considers entries under `ports:` (not `expose:`).
    """
    compose_file = bench_dir / "docker-compose.yml"
    if not compose_file.exists():
        return []

    # Ask Docker for the actual mapped ports after the container is up
    # This handles both fixed mappings AND ephemeral (e.g. `- 80` with no host port)
    results = []
    rc, out, _ = run_cmd(
        ["docker", "compose", "ps", "--format", "json"],
        cwd=bench_dir,
        timeout=15,
    )
    if rc != 0 or not out.strip():
        return []

    for line in out.strip().splitlines():
        try:
            svc = json.loads(line)
        except json.JSONDecodeError:
            continue

        name = svc.get("Service", "")
        publishers = svc.get("Publishers", [])
        if not publishers:
            continue

        for pub in publishers:
            host_port = pub.get("PublishedPort", 0)
            target_port = pub.get("TargetPort", 0)
            proto = pub.get("Protocol", "tcp")
            if host_port and proto == "tcp":
                # Likely a web port if target is 80, 443, 8080, 8000, 3000, etc.
                web_ports = {80, 443, 8080, 8000, 3000, 5000, 8888, 4000, 9000}
                if target_port in web_ports:
                    results.append((name, host_port))

    return results


def check_http(host_port: int, timeout: int = 5) -> tuple[bool, int | None, str]:
    """
    Try HTTP GET on localhost:<host_port>.
    Returns (reachable, status_code, message).
    """
    url = f"http://localhost:{host_port}/"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "bench-runner/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.status, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        # 4xx/5xx still means the server is responding
        return True, e.code, f"HTTP {e.code} (server responded)"
    except urllib.error.URLError as e:
        return False, None, f"Connection failed: {e.reason}"
    except Exception as e:
        return False, None, f"Unexpected error: {e}"


def stop_benchmark(bench_dir: Path) -> None:
    """Bring down the compose stack and clean up volumes."""
    run_cmd(
        ["docker", "compose", "down", "--volumes", "--remove-orphans"],
        cwd=bench_dir,
        timeout=60,
    )


def load_benchmark_meta(bench_dir: Path) -> dict:
    """Read benchmark.json if present."""
    meta_file = bench_dir / "benchmark.json"
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


# ─── Core runner ──────────────────────────────────────────────────────────────

STATUS_SUCCESS  = "SUCCESS"
STATUS_BUILD_FAIL = "BUILD_FAILED"
STATUS_UP_FAIL  = "START_FAILED"
STATUS_UNHEALTHY = "UNHEALTHY"
STATUS_NO_PORT  = "NO_WEB_PORT"
STATUS_HTTP_FAIL = "HTTP_UNREACHABLE"
STATUS_HTTP_OK  = "SUCCESS"


def run_benchmark(bench_dir: Path, bench_name: str) -> dict:
    """
    Full lifecycle for one benchmark:
      build → up → wait for healthy → probe HTTP → tear down
    Returns a result dict.
    """
    result = {
        "benchmark": bench_name,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": None,
        "meta": load_benchmark_meta(bench_dir),
        "ports_probed": [],
        "build_stdout": "",
        "build_stderr": "",
        "up_stdout": "",
        "up_stderr": "",
        "error": None,
    }

    print(f"\n{'='*60}")
    print(f"  {bench_name}")
    print(f"{'='*60}")

    # ── 1. Generate flag the same way common.mk does ──────────────────────────
    bench_upper = bench_name.upper()
    flag_hash_proc = subprocess.run(
        ["bash", "-c", f"echo -n {bench_upper} | openssl sha256 -r | cut -d' ' -f1"],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    flag = f"FLAG{{{flag_hash_proc.stdout.strip()}}}" if flag_hash_proc.returncode == 0 else "FLAG{default}"

    # ── 2. Build ──────────────────────────────────────────────────────────────
    print(f"  [1/4] Building (flag: {flag}) ...")
    rc, stdout, stderr = run_cmd(
        ["docker", "compose", "build",
         "--build-arg", f"FLAG={flag}",
         "--build-arg", f"flag={flag}"],
        cwd=bench_dir,
        timeout=600,
    )
    result["build_stdout"] = stdout[-3000:] if len(stdout) > 3000 else stdout
    result["build_stderr"] = stderr[-3000:] if len(stderr) > 3000 else stderr

    if rc != 0:
        result["status"] = STATUS_BUILD_FAIL
        result["error"] = f"docker compose build exited {rc}"
        print(f"  ✘ BUILD FAILED (exit {rc})")
        print(f"    {stderr.strip()[-300:]}")
        return result

    print(f"  ✔ Build OK")

    # ── 3. Start (--wait honours healthchecks) ────────────────────────────────
    print(f"  [2/4] Starting containers ...")
    rc, stdout, stderr = run_cmd(
        ["docker", "compose", "up", "-d", "--wait"],
        cwd=bench_dir,
        timeout=120,
    )
    result["up_stdout"] = stdout
    result["up_stderr"] = stderr

    if rc != 0:
        result["status"] = STATUS_UP_FAIL
        result["error"] = f"docker compose up --wait exited {rc}"
        print(f"  ✘ START FAILED (exit {rc})")
        print(f"    {stderr.strip()[-300:]}")
        stop_benchmark(bench_dir)
        return result

    print(f"  ✔ Containers healthy")

    # ── 4. Probe HTTP ports ───────────────────────────────────────────────────
    print(f"  [3/4] Probing web ports ...")
    ports = get_web_ports(bench_dir)

    if not ports:
        result["status"] = STATUS_NO_PORT
        result["error"] = "No web ports found in running containers"
        print(f"  ⚠ No web ports detected")
        stop_benchmark(bench_dir)
        return result

    any_ok = False
    for svc_name, host_port in ports:
        reachable, status_code, msg = check_http(host_port)
        probe = {
            "service": svc_name,
            "host_port": host_port,
            "reachable": reachable,
            "http_status": status_code,
            "message": msg,
        }
        result["ports_probed"].append(probe)
        icon = "✔" if reachable else "✘"
        print(f"  {icon} {svc_name} → http://localhost:{host_port}/ → {msg}")
        if reachable:
            any_ok = True

    result["status"] = STATUS_HTTP_OK if any_ok else STATUS_HTTP_FAIL
    if not any_ok:
        result["error"] = "All web ports unreachable over HTTP"

    # ── 5. Tear down ──────────────────────────────────────────────────────────
    print(f"  [4/4] Tearing down ...")
    stop_benchmark(bench_dir)
    print(f"  ✔ Stopped")

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    print(f"  Result: {result['status']}")
    return result


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="XBOW Benchmark Runner — builds, starts, probes, and tears down benchmarks one by one.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Path to the root of the validation-benchmarks repo (contains the 'benchmarks' folder)",
    )
    parser.add_argument(
        "--benchmarks",
        default=None,
        help=(
            "Comma-separated benchmark numbers or ranges to run. "
            "Examples: '78', '1,5,78', '1-10', '1-5,78,95'. "
            "Omit to run ALL discovered benchmarks."
        ),
    )
    parser.add_argument(
        "--log",
        default="bench_results.json",
        help="Path to write the JSON results log (default: bench_results.json)",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo).expanduser().resolve()
    benchmarks_dir = repo_root / "benchmarks"

    if not benchmarks_dir.is_dir():
        print(f"ERROR: 'benchmarks' directory not found inside {repo_root}")
        sys.exit(1)

    # Determine which benchmarks to run
    available = discover_benchmarks(benchmarks_dir)
    if not available:
        print(f"ERROR: No benchmark folders found in {benchmarks_dir}")
        sys.exit(1)

    if args.benchmarks:
        requested = parse_benchmark_selection(args.benchmarks)
        to_run = [n for n in requested if n in available]
        skipped = [n for n in requested if n not in available]
        if skipped:
            print(f"WARNING: These benchmark numbers don't exist on disk and will be skipped: {skipped}")
    else:
        to_run = available

    print(f"\nXBOW Benchmark Runner")
    print(f"Repo:        {repo_root}")
    print(f"Benchmarks:  {len(to_run)} to run")
    print(f"Log output:  {args.log}")
    print(f"Running:     {to_run}")

    all_results = []
    summary = {
        STATUS_SUCCESS:    [],
        STATUS_BUILD_FAIL: [],
        STATUS_UP_FAIL:    [],
        STATUS_UNHEALTHY:  [],
        STATUS_NO_PORT:    [],
        STATUS_HTTP_FAIL:  [],
    }

    for num in to_run:
        name = benchmark_name(num)
        bench_dir = benchmarks_dir / name

        if not bench_dir.is_dir():
            print(f"\nSkipping {name}: directory not found")
            continue

        result = run_benchmark(bench_dir, name)
        all_results.append(result)

        bucket = summary.get(result["status"])
        if bucket is not None:
            bucket.append(name)

        # Write incrementally so a crash doesn't lose everything
        log_path = Path(args.log)
        log_path.write_text(
            json.dumps({"summary": summary, "results": all_results}, indent=2),
            encoding="utf-8",
        )

    # ── Final summary ─────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  FINAL SUMMARY")
    print(f"{'='*60}")
    print(f"  ✔ Success:          {len(summary[STATUS_SUCCESS])}")
    print(f"  ✘ Build failed:     {len(summary[STATUS_BUILD_FAIL])}")
    print(f"  ✘ Start failed:     {len(summary[STATUS_UP_FAIL])}")
    print(f"  ⚠ No web port:      {len(summary[STATUS_NO_PORT])}")
    print(f"  ✘ HTTP unreachable: {len(summary[STATUS_HTTP_FAIL])}")
    print(f"\n  Full results → {args.log}")

    if summary[STATUS_BUILD_FAIL]:
        print(f"\n  Build failures:")
        for b in summary[STATUS_BUILD_FAIL]:
            print(f"    - {b}")
    if summary[STATUS_HTTP_FAIL]:
        print(f"\n  HTTP failures:")
        for b in summary[STATUS_HTTP_FAIL]:
            print(f"    - {b}")


if __name__ == "__main__":
    main()