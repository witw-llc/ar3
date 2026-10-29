# tool-vault

Store encrypted files for agents. Each sender has a separate file area.
`help`, `/public-key`, and `/connect` use plaintext. Other requests are
encrypted to the vault key. Session replies are encrypted to the client's
connection key and signed by the vault.

Use `ar3 tool vault` for administration and client commands.
See [Tool nodes](../../../docs/a8s-tools.md).

## Needs

Use a POSIX system with `openssl` on `PATH`. Set `OPENSSL` to select another
OpenSSL executable.

## Register

Run these commands on the vault host:

```bash
a8s add vault ~/vault-seat tool-vault
ar3 tool vault --node vault init
ar3 tool vault --node vault fingerprint
ar3 tool vault --node vault voucher issue assistant
a8s start vault
```

Give the fingerprint and the first voucher to the intended sender through a
trusted channel. A voucher is a credential. Keep it in a private file.

The vault has no `ALLOW` variable. Each sender must authenticate with a
voucher issued for that sender's name. Senders cannot read each other's files.
Authentication consumes one voucher and returns a replacement.

## Usage

Store and retrieve encrypted files. Replace `<node>` with the vault node name.

From your own node's directory, open two terminals. Run this in the first:

```bash
tells -f
```

Request the public key from the second:

```bash
tell <node> /public-key
```

The reply attaches `vault.pem`. Attachments go to `.files/<msg-ulid>/` in your
node's directory. Compare the key fingerprint with the operator's fingerprint.
Then pin the key and connect:

```bash
ar3 tool vault client pin /absolute/path/vault.pem --fingerprint <fingerprint>
ar3 tool vault client connect --to <node>
```

Use `client open` to read each encrypted reply. Save the full reply body to
`reply.txt`, or pass it on stdin:

```bash
ar3 tool vault client open - --save-voucher voucher.txt < reply.txt
```

Authenticate with the voucher file that the operator supplied:

```bash
ar3 tool vault client auth --to <node> --voucher-file voucher.txt
```

Open the authentication reply with `--save-voucher voucher.txt` before another
operation. This saves the replacement voucher without printing its value.
Then send file commands:

```bash
ar3 tool vault client send /list --to <node>
ar3 tool vault client send "/store notes.txt" --file notes.txt --to <node>
ar3 tool vault client send "/retrieve notes.txt" --to <node>
ar3 tool vault client open - --out retrieved --attachment /absolute/path/notes.txt.enc < reply.txt
ar3 tool vault client send "/delete notes.txt" --to <node>
ar3 tool vault client send /logout --to <node>
```

Replace the attachment path with the file attached to the retrieval reply.
Client send commands send a message and return. They do not wait for a reply.
The client encrypts attachments supplied with `--file`.

### Plaintext commands

| Message | Result |
|---|---|
| `help` | Returns this text and the vault fingerprint. Also accepts `/help`, `--help`, and `/?` as a single-line message. |
| `/help recipe` | Returns the OpenSSL protocol instructions. |
| `/public-key` | Attaches `vault.pem` and returns its fingerprint. |
| `/connect` | Starts a session with one attached RSA public key of at least 2048 bits. Replaces the sender's old session. Encrypts the reply to the new key. |

The vault refuses all other plaintext requests.

### Sealed commands

Send these commands with `ar3 tool vault client send`. Use `client auth` for
authentication so the voucher stays out of command-line arguments.

| Command | Result |
|---|---|
| `/authenticate <voucher>` | Consumes a voucher and returns a replacement. Save the replacement first. |
| `/list` | Lists your files. |
| `/store <name>` | Stores one encrypted attachment. |
| `/retrieve <name>` | Returns the file encrypted to your connection key. |
| `/delete <name>` | Deletes your named file. |
| `/voucher new` | Creates a voucher. |
| `/voucher list` | Counts unused vouchers. |
| `/voucher revoke-all` | Revokes the other vouchers. |
| `/logout` | Ends your session. |

A voucher works once. A sender can have at most 10 unused vouchers.

### Reply

After `/connect`, each reply is encrypted and signed. A command reply contains
the request nonce. A retrieval reply also contains the SHA-256 hash of each
encrypted attachment. `client open` verifies the reply before it saves files.

Plaintext replies contain help, the public key, or a refusal with the node name.

### Options

Set an option with `a8s add ... --KEY=VALUE` or
`a8s vars <node> set KEY VALUE`. Use `--var KEY=VALUE` before a local verb to
change it for one run.

| Variable | Default | Function |
|---|---|---|
| `VAULT_DIR` | `${XDG_DATA_HOME:-~/.local/share}/ar3/vault/<node>` | Selects the vault state directory. |
| `OPENSSL` | `openssl` on `PATH` | Selects the OpenSSL executable. |

## Local commands

Put `--node` before the verb to load a registered vault's settings. Use
`--seat NAME` instead for an unregistered vault. Do not combine them.

| Command after `ar3 tool vault --node vault` | Function |
|---|---|
| `init` | Creates the vault key and pepper if absent. |
| `fingerprint` | Prints the vault key fingerprint. |
| `admin add NAME` | Adds an alert recipient. |
| `admin rm NAME` | Removes an alert recipient. |
| `admin list` | Lists alert recipients. |
| `voucher issue NAME` | Creates and prints a voucher for the sender. Treat the output as a credential. |
| `voucher purge NAME` | Revokes the sender's unused vouchers. |
| `seat list` | Lists connected senders. |
| `seat disconnect NAME` | Ends the sender's connection. |

Administrative verbs accept `--dir PATH` after the verb to select another state
directory. Use `--help` to inspect an option without a vault operation:

```bash
ar3 tool vault --help
ar3 tool vault voucher --help
ar3 tool vault client --help
```

Client commands run in the sender's environment. They use `--to NAME` to
select the destination; they do not need the vault registered on that machine. The client refuses
`--node` and `--seat`; those selectors apply to local vault administration.

| Client action | Arguments and options |
|---|---|
| `pin` | `PEM --fingerprint FINGERPRINT` |
| `connect` | `--to NAME` |
| `auth` | `--to NAME --voucher-file PATH` |
| `send` | `COMMAND [--file PATH] --to NAME` |
| `seal` | `COMMAND [--file PATH]`; prints the encrypted body and attachment paths without sending. |
| `open` | `- [--save-voucher PATH] [--out DIR] [--attachment PATH]...`; reads the reply from stdin. |

`A8S_VAULT_SEAT` sets the default destination; otherwise it is `vault`.
`A8S_VAULT_CLIENT` selects the client state directory; otherwise it is
`~/.cache/a8s-vault-client`. `A8S_VAULT_FINGERPRINT` can set the expected key
fingerprint. Use separate client state directories for separate vaults or
sender identities.

## State

The vault stores its key, pepper, and index in
`${XDG_DATA_HOME:-~/.local/share}/ar3/vault/<node>/`. The directory has mode
0700. Encrypted files are in `data/`. The client state includes the pinned
vault key, its connection key, and pending request nonces.

## Platforms

The vault host supports POSIX with OpenSSL. The client includes Windows file
locking, but the host's supported platform is POSIX.

## Try it by hand

Create an unregistered test vault in a separate local state directory:

```bash
ar3 tool vault --seat vault-test init
ar3 tool vault --seat vault-test fingerprint
ar3 tool vault --seat vault-test -- help
```

Put `--` before a message. A command such as `init` before `--` is a local
administrative operation. A message cannot run that operation.

## Protocol reference

A client connects with a temporary RSA public key of at least 2048 bits.
An encrypted request has the prefix `A8SV1:` followed by a base64 blob sealed
to the vault key. Its plaintext has this structure:

```json
{"req":"<request JSON>","sig":"<base64 SHA-256 signature of the exact request string>"}
```

The request JSON contains:

```json
{"v":1,"cmd":"<command>","conn":"<SHA-256 of client public-key DER>","ts":"<UTC ISO-8601>","nonce":"<random hex>","files":{"<attachment>":"<SHA-256>"}}
```

The client signs with its connection private key. Use the client commands to
create and verify these messages. Request `/help recipe` for the raw OpenSSL
instructions.

## Errors

| Condition | Action |
|---|---|
| No pinned key | Request `/public-key`. Verify the fingerprint, then run `client pin`. |
| No connection | Run `client connect`. |
| Authentication refused | Check the sender identity. Ask the operator for a new voucher if no unused voucher remains. |
| Reply belongs to an old connection | Do not use it. A new connection has a different key. |
| OpenSSL unavailable | Install OpenSSL or set `OPENSSL` to its executable path. |

Local commands return 0 on success and 1 on an operation failure. Invalid
command-line syntax returns 2. `client open` returns 3 for a reply encrypted
to an old connection key.

## Trust

A process that runs as the vault's OS user can read the private key. Use a
separate OS user to restrict this access. Keep vouchers and client private
keys out of messages, command-line arguments, and shared files.
