"""
Publish data/tiles/ to the Cloudflare R2 bucket the app reads from.

A Pages site may weigh 1 GB and a country of tiles, rebuilt now and then,
outgrows that and bloats the git history besides; R2 has no egress fees and
speaks the S3 API. Only what changed is sent: an object whose ETag (the MD5
of a single-part upload) matches the local file is left alone.

    pipeline/.venv/bin/python pipeline/publish_tiles.py --dry-run
    pipeline/.venv/bin/python pipeline/publish_tiles.py
    pipeline/.venv/bin/python pipeline/publish_tiles.py --setup-cors   # once, after creating the bucket

Credentials never enter the repo: ``R2_ACCOUNT_ID``, ``R2_ACCESS_KEY_ID``,
``R2_SECRET_ACCESS_KEY`` (an R2 API token with object read & write on the
bucket) and ``R2_BUCKET``, from the environment or from ``pipeline/.env``,
which is ignored by git.

JSON is stored gzipped with ``Content-Encoding: gzip`` — the browser
inflates it on the way in — which takes an air climatology from 214 KB to
49 KB; PNGs are compressed already and go as they are.

Order matters to a visitor loading mid-upload: product files go first, then
each tile's meta.json, then index.json, so nothing ever lists a file that is
not there yet. index.json and meta.json are sent as ``no-cache`` — they are
tiny and are what says a tile was rebuilt; the products are cached for good,
since the app asks for them with the generation stamp in the query.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import logging
import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tiles import DATA_DIR  # noqa: E402
import netdns  # noqa: E402,F401  (falls back to DNS over HTTPS when the system resolver drops a name)

log = logging.getLogger("publish")

TYPES = {".png": "image/png", ".json": "application/json", ".webp": "image/webp"}
FRESH = "no-cache"
FOREVER = "public, max-age=31536000, immutable"


def load_env(path: Path = Path(__file__).resolve().parent / ".env") -> None:
    """KEY=VALUE lines from pipeline/.env, under whatever the shell already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def client():
    load_env()
    missing =[k for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET") if not os.environ.get(k)]
    if missing:
        sys.exit(f"missing {', '.join(missing)} in the environment")
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(retries={"max_attempts": 8, "mode": "adaptive"}),
    )
    return s3, os.environ["R2_BUCKET"]


def remote_etags(s3, bucket: str) -> dict[str, str]:
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            out[obj["Key"]] = obj["ETag"].strip('"')
    return out


def local_files() -> list[Path]:
    """Every file under data/tiles, products first, metas next, index last."""
    files = [p for p in DATA_DIR.rglob("*") if p.is_file() and p.suffix in TYPES]
    rank = {"meta.json": 1, "index.json": 2}
    return sorted(files, key=lambda p: (rank.get(p.name, 0), str(p)))


def body(p: Path) -> bytes:
    """What is stored for a file: JSON gzipped (deterministically, so that an
    unchanged file has an unchanged ETag), anything else as it is."""
    data = p.read_bytes()
    return gzip.compress(data, 9, mtime=0) if p.suffix == ".json" else data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="say what would be sent or deleted, send nothing")
    ap.add_argument("--prune", action="store_true",
                    help="delete objects with no local file (only from a machine holding every tile)")
    ap.add_argument("--setup-cors", action="store_true", help="let any origin GET the bucket, then exit")
    args = ap.parse_args()
    s3, bucket = client()

    if args.setup_cors:
        s3.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": [{
            "AllowedOrigins": ["*"], "AllowedMethods": ["GET", "HEAD"],
            "AllowedHeaders": ["*"], "MaxAgeSeconds": 86400,
        }]})
        log.info("CORS set on %s: GET and HEAD from any origin", bucket)
        return

    remote = remote_etags(s3, bucket)
    files = local_files()
    sent = sent_bytes = 0
    for p in files:
        key = p.relative_to(DATA_DIR).as_posix()
        data = body(p)
        if remote.get(key) == hashlib.md5(data).hexdigest():
            continue
        size = len(data)
        log.info("%s %s (%.0f KB)", "would send" if args.dry_run else "send", key, size / 1e3)
        if not args.dry_run:
            extra = {"ContentEncoding": "gzip"} if p.suffix == ".json" else {}
            s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=TYPES[p.suffix],
                          CacheControl=FRESH if p.name in ("meta.json", "index.json") else FOREVER, **extra)
        sent += 1
        sent_bytes += size

    gone = sorted(set(remote) - {p.relative_to(DATA_DIR).as_posix() for p in files})
    if gone and args.prune:
        for i in range(0, len(gone), 1000):
            log.info("%s %d objects", "would delete" if args.dry_run else "delete", len(gone[i:i + 1000]))
            if not args.dry_run:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in gone[i:i + 1000]]})
    elif gone:
        log.info("%d objects in the bucket have no local file (kept; --prune deletes them)", len(gone))
    log.info("%d of %d files %s, %.1f MB", sent, len(files), "to send" if args.dry_run else "sent", sent_bytes / 1e6)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    main()
