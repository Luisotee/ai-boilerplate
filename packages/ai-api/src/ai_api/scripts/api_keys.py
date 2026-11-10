"""Issue, list and revoke keys for the OpenAI-compatible ``/v1`` API.

Run where the database is reachable, e.g. inside the API container:

    docker compose exec api python -m ai_api.scripts.api_keys create --name "Ana web"
    docker compose exec api python -m ai_api.scripts.api_keys create --name "Ana" --jid 5511999999999@s.whatsapp.net
    docker compose exec api python -m ai_api.scripts.api_keys list
    docker compose exec api python -m ai_api.scripts.api_keys revoke sk-AbCdEfG

``create`` prints the key once; only its hash is stored. Without ``--jid`` the
key gets a new ``web:<id>`` user, which the person can ``/link`` to their
WhatsApp or Telegram chat from the app. See ``packages/ai-api/docs/openai-api.md``.
"""

from __future__ import annotations

import argparse
import sys

from ..database import SessionLocal, init_db
from ..services.api_keys import create_key, list_keys, revoke_key


def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "-"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ai_api.scripts.api_keys")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="issue a key (printed once)")
    create.add_argument("--name", required=True, help="who or what the key is for")
    create.add_argument(
        "--jid", help="attach to this existing private chat instead of a new web user"
    )
    sub.add_parser("list", help="list keys (never the keys themselves)")
    revoke = sub.add_parser("revoke", help="revoke a key by id or prefix")
    revoke.add_argument("key", help="key id, or the prefix shown by `list`")
    args = parser.parse_args(argv)

    init_db()  # creates the api_keys table on a deployment that predates it
    db = SessionLocal()
    try:
        if args.command == "create":
            issued = create_key(db, args.name, jid=args.jid)
            sys.stdout.write(
                f"Key for {issued.record.name} (user {issued.user.whatsapp_jid}):\n\n"
                f"  {issued.key}\n\n"
                "Store it now: it is not shown again.\n"
            )
        elif args.command == "list":
            for record, user in list_keys(db):
                state = "revoked" if record.revoked_at else "active"
                jid = user.whatsapp_jid if user else "?"
                sys.stdout.write(
                    f"{record.id}  {record.key_prefix}…  {state:7}  {record.name}  {jid}  "
                    f"created {_fmt(record.created_at)}  last used {_fmt(record.last_used_at)}\n"
                )
        else:
            record = revoke_key(db, args.key)
            sys.stdout.write(f"Revoked {record.key_prefix}… ({record.name}).\n")
    except ValueError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
