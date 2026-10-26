from __future__ import annotations

import argparse
import logging
import sys

from freeaihub.app import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description="Free AI Hub — free-tier OpenAI-compatible gateway")
    parser.add_argument("--host", default=None, help="bind host (default: settings or 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default: settings or 8899)")
    parser.add_argument("--no-refresh", action="store_true", help="do not probe providers at startup")
    parser.add_argument("--debug", action="store_true", help="Flask debug mode (not for LAN use)")
    args = parser.parse_args()

    app = create_app(start_background=not args.no_refresh)
    store = app.extensions["freeaihub"]
    settings = store["settings"]
    host = args.host or str(settings.get("host") or "0.0.0.0")
    port = args.port or int(settings.get("port") or 8899)
    key = settings.effective_api_key()
    password_hint = "admin (default; change in Settings)" if settings.verify_dashboard_password("admin") else "set"

    print("=" * 68)
    print("  Free AI Hub")
    print(f"  Dashboard : http://127.0.0.1:{port}/")
    print(f"  API base  : http://127.0.0.1:{port}/v1")
    print(f"  Password  : {password_hint}")
    print(f"  API key   : {key or '(missing)'}")
    print("  Scope     : project data/ catalogs, local keys and explicit provider env vars")
    print("=" * 68)
    # Keep the console focused on our 127.0.0.1 URLs; suppress Werkzeug's
    # "Running on all addresses" INFO banner (errors still surface).
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app.run(host=host, port=port, debug=args.debug, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
