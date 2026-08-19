#!/usr/bin/env python3
"""Manage who may use the FemtoMind MCP endpoint.

    ./femtoadmin.py invite "Nobuo Sato" [cluster-username]
    ./femtoadmin.py list
    ./femtoadmin.py revoke u_ab12cd34

An invite is the whole onboarding: hand someone the code and the server URL,
and they are set up. Revoking kills their live sessions too, not just future
sign-ins, so a departed collaborator loses access immediately.
"""

import sys
from pathlib import Path

# Run from anywhere: the auth module lives in the package root above scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import femtomind_auth as auth

USAGE = __doc__


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(USAGE)
        return 1
    cmd = argv[1]

    if cmd == "invite":
        if len(argv) < 3:
            print("usage: femtoadmin.py invite \"Display Name\" [cluster-username]")
            return 1
        rec = auth.mint_invite(argv[2], argv[3] if len(argv) > 3 else "")
        host = auth.PUBLIC_HOST
        print(f"\n  {rec['display_name']}  ({rec['user_id']})\n")
        print(f"    access code : {rec['invite_code']}")
        print(f"    server URL  : https://{host}/mcp\n")
        print("  Send them both. In ChatGPT: add a connector, choose Server URL,")
        print("  set Authentication to OAuth, then paste the code on the sign-in page.\n")
        return 0

    if cmd == "list":
        rows = auth.list_users()
        if not rows:
            print("  no collaborators yet — run: femtoadmin.py invite \"Name\"")
            return 0
        print(f"\n  {'user_id':<14} {'name':<20} {'code':<17} {'cluster':<10} {'state':<8} last seen")
        print("  " + "-" * 86)
        for r in rows:
            print(f"  {r['user_id']:<14} {(r['display_name'] or '')[:19]:<20} "
                  f"{r['invite_code']:<17} {(r['hpc_username'] or '-'):<10} "
                  f"{'REVOKED' if r['revoked'] else 'active':<8} {r['last_seen_at'] or 'never'}")
        print()
        return 0

    if cmd == "revoke":
        if len(argv) < 3:
            print("usage: femtoadmin.py revoke <user_id>")
            return 1
        print("  revoked" if auth.revoke_user(argv[2]) else "  no such user")
        return 0

    print(USAGE)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
