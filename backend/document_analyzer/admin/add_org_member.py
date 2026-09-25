"""Admin CLI: grant an already-signed-up user access to an org.

There is no self-serve signup flow (design.md §1a) — a new auth.users row grants
no access to any table until a row exists in org_members. This wraps the
service-role-only `add_org_member_by_email` / `list_org_members` RPCs
(supabase/migrations/20260925000000_add_org_member_admin_helpers.sql) so that
provisioning doesn't require hand-editing tables in the Supabase dashboard.

Usage:
    uv run document-analyzer-add-org-member <email> <org_id>
    uv run document-analyzer-add-org-member --list
"""

import sys

from dotenv import load_dotenv

load_dotenv()  # must run before supabase_client reads SUPABASE_* env vars

from ..supabase_client import get_service_role_client  # noqa: E402


def _list_members() -> None:
    client = get_service_role_client()
    result = client.rpc("list_org_members").execute()
    rows = result.data or []
    if not rows:
        print("No org_members rows yet.")
        return
    for row in rows:
        print(f"{row['org_id']}  {row['email']}  ({row['user_id']}, joined {row['created_at']})")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    if args[0] == "--list":
        _list_members()
        return
    if len(args) != 2:
        print("Usage: document-analyzer-add-org-member <email> <org_id>", file=sys.stderr)
        print("       document-analyzer-add-org-member --list", file=sys.stderr)
        sys.exit(1)

    email, org_id = args
    client = get_service_role_client()
    result = client.rpc("add_org_member_by_email", {"p_email": email, "p_org_id": org_id}).execute()
    print(f"Added {email} ({result.data}) to org {org_id}")


if __name__ == "__main__":
    main()
