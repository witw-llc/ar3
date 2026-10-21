"""CLI: vault administration (local only), the a8s wake entry point, and the agent client."""
import argparse
import json
import os
import sys
import time

from . import client
from . import crypto
from . import handler
from . import vault as vaultlib


def _vault(args):
    return vaultlib.Vault(args.dir, args.node).load()


def _admin(args):
    v = _vault(args)
    with v.db:
        if args.action == "add":
            v.admin_add(args.seat.lower())
        elif args.action == "rm":
            v.admin_rm(args.seat.lower())
    for seat in v.admins():
        print(seat)
    return 0


def _voucher(args):
    v = _vault(args)
    seat = args.seat.lower()
    with v.db:
        if args.action == "issue":
            print(v.issue(seat))
        else:
            print(f"purged {v.purge(seat)} voucher(s) for {seat}")
    return 0


def _seat(args):
    v = _vault(args)
    if args.action == "disconnect":
        with v.db:
            v.disconnect(args.seat.lower())
        print(f"disconnected {args.seat.lower()}")
        return 0
    for row in v.seats():
        active = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["last_active"] or 0))
        print(f"{row['seat']:<24} {'authed' if row['authed'] else 'connected':<10} "
              f"vouchers={row['live']:<3} conn={(row['conn_fpr'] or '')[:16]} last={active}")
    return 0


def _client(args):
    to = args.to or os.environ.get("A8S_VAULT_SEAT", "vault")
    if args.action == "pin":
        print(client.pin(args.pem, args.fingerprint))
    elif args.action == "connect":
        print(f"sent /connect to {to} with key {client.connect(to)}")
    elif args.action == "send":
        client.send(to, args.cmd, args.file)
    elif args.action == "auth":
        voucher = args.voucher
        if not voucher:
            with open(args.voucher_file) as handle:
                voucher = handle.read().strip()
        client.send(to, f"/authenticate {voucher}")
    elif args.action == "seal":
        body, files = client.seal_command(args.cmd, args.file)
        print(json.dumps({"body": body, "attach": files}))
    elif args.action == "open":
        message = sys.stdin.read() if args.message in (None, "-") else args.message
        reply, plain, written = client.open_reply(
            message, args.out, args.save_voucher, args.attachment or ())
        if reply is None:
            print(plain)
            return 0
        if args.save_voucher and reply.get("voucher"):
            reply["voucher"] = f"(saved to {args.save_voucher})"
        print(json.dumps(reply, indent=2, sort_keys=True))
        for path in written:
            print(f"wrote {path}")
        return 0 if reply.get("ok") else 1
    return 0


def _handle(args):
    """The a8s wake: every sender is served; the voucher model gates access."""
    if not args.sender:
        print("vault: senderless wake refused", file=sys.stderr)
        return 0
    if args.openssl:
        os.environ["OPENSSL"] = args.openssl
    return handler.handle(args.seat, args.sender, args.message, args.dir)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="vault", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    where = argparse.ArgumentParser(add_help=False)
    where.add_argument("--node", default="vault", help="the vault node's name (default vault)")
    where.add_argument("--dir", default=None, help="state root (default $VAULT_DIR, else ${XDG_DATA_HOME:-~/.local/share}/ar3/vault/<node>)")
    sub.add_parser("init", parents=[where], help="create the vault keypair and pepper")
    sub.add_parser("fingerprint", parents=[where], help="print the vault key fingerprint")
    p = sub.add_parser("handle", help="a8s wake entry point")
    p.add_argument("--seat", default="vault", help="this node's name ($RECIPIENT)")
    p.add_argument("--from", dest="sender", default="")
    p.add_argument("--dir", default=None, help="state root (the VAULT_DIR var)")
    p.add_argument("--openssl", default=None, help="openssl binary (the OPENSSL var)")
    p.add_argument("message", nargs="?", default="")
    p = sub.add_parser("admin", parents=[where], help="seats that receive alerts")
    p.add_argument("action", choices=["add", "rm", "list"])
    p.add_argument("seat", nargs="?")
    p = sub.add_parser("voucher", parents=[where], help="issue a voucher, or purge a seat's vouchers")
    p.add_argument("action", choices=["issue", "purge"])
    p.add_argument("seat")
    p = sub.add_parser("seat", parents=[where], help="list seats or drop a session")
    p.add_argument("action", choices=["list", "disconnect"])
    p.add_argument("seat", nargs="?")
    p = sub.add_parser("client", help="agent side: pin, connect, send, auth, seal, open")
    p.add_argument("action", choices=["pin", "connect", "send", "auth", "seal", "open"])
    p.add_argument("arg", nargs="?", help="pin: vault.pem; send/seal: command; open: message|-")
    p.add_argument("--to", help="vault seat name (default $A8S_VAULT_SEAT or 'vault')")
    p.add_argument("--fingerprint", help="pin: expected vault fingerprint")
    p.add_argument("--file", help="send/seal: file to store (sealed for you)")
    p.add_argument("--voucher")
    p.add_argument("--voucher-file")
    p.add_argument("--save-voucher", help="open: write a returned voucher here atomically")
    p.add_argument("--out", help="open: directory for retrieved files")
    p.add_argument("--attachment", action="append", help="open: extra attached file path")

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            v = vaultlib.Vault(args.dir, args.node)
            created = v.init()
            print(("created " if created else "existing ") + f"vault key {v.fpr}")
            print(f"config {v.config}\ndata   {v.data}")
            return 0
        if args.command == "fingerprint":
            print(_vault(args).fpr)
            return 0
        if args.command == "handle":
            return _handle(args)
        if args.command in ("admin", "seat") and args.action not in ("list",) and not args.seat:
            parser.error(f"{args.command} {args.action} needs a seat")
        if args.command == "admin":
            return _admin(args)
        if args.command == "voucher":
            return _voucher(args)
        if args.command == "seat":
            return _seat(args)
        if args.command == "client":
            args.pem = args.cmd = args.message = args.arg
            if args.action in ("pin", "send", "seal") and not args.arg:
                parser.error(f"client {args.action} needs an argument")
            if args.action == "auth" and not (args.voucher or args.voucher_file):
                parser.error("client auth needs --voucher or --voucher-file")
            return _client(args)
    except client.StaleReply as exc:
        print(f"vault: {exc}", file=sys.stderr)
        return 3
    except (vaultlib.VaultError, client.ClientError, crypto.CryptoError, OSError) as exc:
        print(f"vault: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 0
