"""Fixed worker entry point: ``python -m graphify_worker <command>``.

Commands:
  analyse                 run extraction using /control/request.json (default)
  capabilities            print the capability manifest (used at image build time)
  show-capabilities       print the manifest generated at build time (fast; no smoke parse)
  check-capabilities F    fail if manifest F advertises a parser that does not work
  netcheck URL            outbound connectivity diagnostic (acceptance test only)
  hold SECONDS            sleep; deterministic diagnostic for timeout/cancel tests
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

BUILT_CAPABILITIES = "/opt/graphify_worker/capabilities.json"


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "analyse"
    if command == "analyse":
        from .adapter import main_analyse

        return main_analyse()
    if command == "capabilities":
        from .capabilities import main_capabilities

        return main_capabilities()
    if command == "show-capabilities":
        with open(BUILT_CAPABILITIES, encoding="utf-8") as fh:
            sys.stdout.write(fh.read())
        return 0
    if command == "check-capabilities" and len(argv) == 2:
        from .capabilities import check_manifest

        with open(argv[1], encoding="utf-8") as fh:
            problems = check_manifest(json.load(fh))
        for problem in problems:
            print(f"capability problem: {problem}", file=sys.stderr)
        return 1 if problems else 0
    if command == "netcheck" and len(argv) == 2:
        url = argv[1]
        if not url.startswith(("https://", "http://")):
            print("netcheck requires an http(s) URL", file=sys.stderr)
            return 2
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310 - fixed scheme
                print(json.dumps({"url": url, "status": resp.status}))
                return 0
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"url": url, "error": type(exc).__name__}))
            return 1
    if command == "hold" and len(argv) == 2:
        time.sleep(float(argv[1]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
