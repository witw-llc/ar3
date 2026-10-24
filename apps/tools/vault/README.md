# tool-vault

An encrypted file store for agents. `help`, `/public-key` and `/connect` travel
in plaintext. Every other message is sealed to the vault's key, and every reply
is sealed to the agent's throwaway key and signed. The crypto runs through the
`openssl` command.

This page is the reference for the node. The tool overview is in
[docs/a8s-tools.md](../../../docs/a8s-tools.md).

## Needs

`openssl` on the node's `PATH`, or named by the `OPENSSL` var.

## Register

```bash
a8s add vault ~/vault-seat tool-vault
python3 apps/tools/vault init --node vault
python3 apps/tools/vault voucher issue alice --node vault
```

There is no ACL var. Every sender is served under its own section, keyed by its
name, and no sender sees another's files. The operator issues the first voucher
for each name on the vault host. The agent pins the key, connects, and
authenticates with that voucher. The vault hands back a fresh voucher each time.

## Usage

An encrypted file store for agents. `help`, `/public-key` and `/connect` travel
in plaintext. Every other message is sealed to the vault's key, and every reply
is sealed to the agent's throwaway key and signed. Replace `<node>` with the
vault's node name.

Work in real time with two terminals. In the first, inside the directory of
your own node:

```bash
tells -f
```

In the second:

```bash
tell <node> help
tell <node> /public-key
python3 apps/tools/vault client pin vault.pem --fingerprint <F>
python3 apps/tools/vault client connect --to <node>
```

The reply to `/public-key` attaches `vault.pem`. Attachments land under your
node's `.files/<msg-ulid>/`. Check the fingerprint against the one the operator
gave you before you pin it. The `client` subcommands (`pin`, `connect`, `send`,
`auth`, `seal`, `open`) seal and open the messages for you. The raw `openssl`
recipe for sealing is in the answer to `tell <node> "/help recipe"`.

### Plaintext commands

Open to every sender:

| Message | Meaning |
|---|---|
| `help` | This text, with the vault key fingerprint. Also `/help`, `--help`, `/?`. The message must be that one line. |
| `/help recipe` | The `openssl` recipe for sealing and opening. |
| `/public-key` | Attaches `vault.pem` and names its fingerprint. |
| `/connect` | With one attached public key (at least 2048 bits RSA): starts a session. A new `/connect` wipes the old session. The reply is sealed to that key. |

Any other plaintext message is refused, and the refusal carries this text.

### The sealed protocol

1. `tell <node> /public-key` returns `vault.pem`. Check its fingerprint.
2. `tell <node> --attach me.pem /connect` sends a throwaway RSA public key
   (2048 bits or more). Replies are sealed to it from now on.
3. `tell <node> "A8SV1:<base64 of a blob sealed to vault.pem>"` sends a command.
   The blob's plaintext is `{"req":"<request JSON>", "sig":"<base64 SHA-256
   signature of that exact string by me.pem's private key>"}`. The request JSON is:

   ```
   {"v":1, "cmd":"/authenticate <voucher>", "conn":"<sha256 of me.pem DER>",
    "ts":"<UTC ISO-8601>", "nonce":"<random hex>", "files":{"<attachment>":"<sha256>"}}
   ```

   Replies echo the nonce. For `/retrieve` they also give the sha256 of each
   sealed attachment.
4. `/authenticate` burns the voucher and returns the next one. Save it before
   anything else.

### Sealed commands

After `/connect` and an operator-issued voucher:

| Command | Meaning |
|---|---|
| `/authenticate <voucher>` | Burns the voucher and returns the next one. Save it first. |
| `/list` | Lists your files. |
| `/store <name>` | Stores the one attached file, sealed to the vault key. |
| `/retrieve <name>` | Returns the file, sealed to your key. |
| `/delete <name>` | Deletes a file. |
| `/voucher new`, `list`, `revoke-all` | Mints a voucher, counts them, or burns all others. |
| `/logout` | Ends the session. |

A voucher works once. A seat holds at most 10 unused vouchers. The first one
comes from the vault's operator: `python3 apps/tools/vault voucher issue <seat>`
on the vault host.

### Reply

After `/connect`, every reply is sealed and signed. The vault echoes the nonce
of the request. For `/retrieve` it also gives the sha256 of each sealed
attachment. Plaintext replies are this text, the key attachment, or a refusal
that starts with the node name.

### Options

Set with `a8s add ... --KEY=VALUE` or `a8s vars <node> set KEY VALUE`.

| Var | Default | Meaning |
|---|---|---|
| `VAULT_DIR` | `${XDG_DATA_HOME:-~/.local/share}/ar3/vault/<node>` | Moves the whole state root. |
| `OPENSSL` | `openssl` on `PATH` | The `openssl` binary. |

The admin commands (`init`, `fingerprint`, `admin`, `voucher`, `seat`) take
`--node` and `--dir` to find the state root.

## State

The key, pepper and index live in
`${XDG_DATA_HOME:-~/.local/share}/ar3/vault/<node>/` (mode 0700), and the
sealed files in its `data/` folder.

## Platforms

POSIX with `openssl`.

## Try it by hand

Run one wake and print the reply:

```bash
VAULT_DIR=/tmp/vault-try python3 apps/tools/vault init
ar3 tool vault --var VAULT_DIR=/tmp/vault-try -- help
```

The tool has its own CLI: `init`, `fingerprint`, `admin add|rm|list`,
`voucher issue|purge`, `seat list|disconnect` for the operator, and `client` for
the agent.

## Trust

The vault shares a Unix user with whoever runs the node. Anyone who can read
that user's files can read the key. Run it as its own user when that matters.
