"""Amazon S3 storage service (and any S3-compatible endpoint).

Config URL is the bucket, optionally with a key prefix:

  a8s storage mybucket s3://my-bucket
  a8s storage mybucket s3://my-bucket/a8s-files --region us-west-2

Wire behaviour:

  store(src)     PUT  s3://<bucket>/<prefix>/<token>/<filename>
                 returns a presigned GET URL valid for `presign_hours`
  retrieve(url)  GET  the presigned URL, or an `s3://bucket/key` URI

The returned URL is presigned rather than an `s3://` URI so a receiver needs
no AWS credentials of its own — the capability to fetch travels inside the
envelope, exactly like the tempfile-class services. A cluster whose machines
all hold credentials still fetches presigned URLs fine, so there is one code
path rather than a mode switch.

Keys are namespaced under `prefix` (default `a8s`) so the bucket owner can
point a lifecycle rule at that prefix and let expiry be the bucket's job.
Nothing here deletes: a storage service that reaches back into a bucket to
remove objects is a foot nuke, and S3 already has the feature.

boto3 is imported lazily and is an on-demand dependency (`requirements/
a8s-s3.txt`, fetched with `ar3 deps a8s-s3`), pulled in via `ar3.deps.
use_group` rather than vendored. It carries the standard credential chain —
env vars, shared config, SSO, instance and container roles — which is the
whole reason to depend on it rather than sign requests by hand: an operator
who grants a machine an IAM role gets working uploads with no a8s-side
secret handling. a8s never reads, stores, or logs a credential.
"""
from __future__ import annotations

import secrets
import urllib.parse
from pathlib import Path
from typing import Any

from services import StorageError, StorageService, resolve_prefix

_KNOWN_OPTS: set[str] = {
    "region",
    "prefix",
    "presign_hours",
    "timeout_s",
    "endpoint_url",
    "profile",
}

DEFAULT_PREFIX = "a8s"
DEFAULT_PRESIGN_HOURS = 24
DEFAULT_TIMEOUT_S = 60
MAX_PRESIGN_HOURS = 168  # S3 SigV4 ceiling is 7 days


def _split_bucket_url(url: str) -> tuple[str, str]:
    """`s3://bucket/some/prefix` -> ("bucket", "some/prefix")."""
    parsed = urllib.parse.urlsplit(url.strip())
    bucket = (parsed.netloc or "").strip()
    prefix = (parsed.path or "").strip("/")
    return bucket, prefix


class S3Service(StorageService):
    def __init__(self, name: str, *, url: str, **opts: Any) -> None:
        unknown = set(opts) - _KNOWN_OPTS
        if unknown:
            raise ValueError(
                f"storage {name!r}: unknown option(s) {', '.join(sorted(unknown))}"
            )
        bucket, url_prefix = _split_bucket_url(url)
        if not bucket:
            raise ValueError(
                f"storage {name!r}: url must name a bucket, e.g. s3://my-bucket"
            )
        self._name = name
        self._bucket = bucket
        self._prefix = resolve_prefix(opts, url_prefix or DEFAULT_PREFIX)
        self._region = (opts.get("region") or "").strip() or None
        self._endpoint_url = (opts.get("endpoint_url") or "").strip() or None
        self._profile = (opts.get("profile") or "").strip() or None
        # `or` would read an explicit 0 as "unset" and silently substitute the
        # default, so absence is checked rather than falsiness.
        raw_timeout = opts.get("timeout_s")
        self._timeout_s = int(DEFAULT_TIMEOUT_S if raw_timeout is None else raw_timeout)
        if self._timeout_s < 1:
            raise ValueError(f"storage {name!r}: timeout_s must be positive")
        raw_hours = opts.get("presign_hours")
        hours = int(DEFAULT_PRESIGN_HOURS if raw_hours is None else raw_hours)
        if not 1 <= hours <= MAX_PRESIGN_HOURS:
            raise ValueError(
                f"storage {name!r}: presign_hours must be 1..{MAX_PRESIGN_HOURS}"
            )
        self._presign_hours = hours
        self._client_cache: Any = None

    @property
    def id(self) -> str:
        return self._name

    @classmethod
    def supports_config_url(cls, url: str) -> bool:
        try:
            return urllib.parse.urlsplit(url.strip()).scheme.lower() == "s3"
        except ValueError:
            return False

    def _client(self) -> Any:
        if self._client_cache is not None:
            return self._client_cache
        from ar3.deps import use_group
        use_group("a8s-s3")
        try:
            import boto3
            from botocore.config import Config
        except ImportError as e:
            raise StorageError(
                "boto3 is required for s3 storage — run `ar3 deps a8s-s3`"
            ) from e
        session_args = {"profile_name": self._profile} if self._profile else {}
        try:
            session = boto3.session.Session(**session_args)
            self._client_cache = session.client(
                "s3",
                region_name=self._region,
                endpoint_url=self._endpoint_url,
                config=Config(
                    connect_timeout=self._timeout_s,
                    read_timeout=self._timeout_s,
                    retries={"max_attempts": 2},
                ),
            )
        except Exception as e:
            raise StorageError(f"s3 client init failed: {e}") from e
        return self._client_cache

    def _key_for(self, filename: str) -> str:
        token = secrets.token_hex(8)
        safe = Path(filename).name
        return f"{self._prefix}/{token}/{safe}" if self._prefix else f"{token}/{safe}"

    def store(self, src: Path, *, msg_id: str = "") -> str:
        client = self._client()
        key = self._key_for(src.name)
        try:
            client.upload_file(str(src), self._bucket, key)
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=self._presign_hours * 3600,
            )
        except StorageError:
            raise
        except Exception as e:
            raise StorageError(f"s3 upload failed for {src.name}: {e}") from e

    def _key_from_url(self, url: str) -> str | None:
        """Return the object key when `url` addresses this bucket, else None.

        Accepts the `s3://bucket/key` form and the https forms boto3 presigns
        into — virtual-host (`bucket.s3.region.amazonaws.com/key`) and
        path-style (`host/bucket/key`, which custom endpoints use)."""
        try:
            parsed = urllib.parse.urlsplit(url.strip())
        except ValueError:
            return None
        path = parsed.path.lstrip("/")
        if parsed.scheme.lower() == "s3":
            return path if parsed.netloc == self._bucket and path else None
        if parsed.scheme.lower() not in ("http", "https"):
            return None
        host = parsed.netloc.split("@")[-1].split(":")[0]
        if host.startswith(f"{self._bucket}."):
            return path or None
        first, _, rest = path.partition("/")
        if first == self._bucket and rest:
            return rest
        return None

    def delete(self, url: str) -> bool:
        key = self._key_from_url(url)
        if key is None:
            return False
        try:
            self._client().delete_object(Bucket=self._bucket, Key=key)
        except Exception:
            return False
        return True

    def retrieve(self, url: str, dest: Path) -> bool:
        key = self._key_from_url(url)
        if key is None:
            return False
        try:
            parsed = urllib.parse.urlsplit(url.strip())
        except ValueError:
            return False
        if parsed.scheme.lower() in ("http", "https"):
            from settings import get_int
            from services.http_get import http_get_url_to_path

            return http_get_url_to_path(
                url,
                dest,
                timeout_s=self._timeout_s,
                max_bytes=get_int("max_file_bytes"),
            )
        if parsed.scheme.lower() != "s3":
            return False
        client = self._client()
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(self._bucket, urllib.parse.unquote(key), str(dest))
        except StorageError:
            raise
        except Exception as e:
            raise StorageError(f"s3 download failed for {url}: {e}") from e
        return True
