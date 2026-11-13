"""File storage: an S3 bucket, or anything that speaks the S3 API.

Files are passed between the platform and this service as storage keys, never as bytes in a
request. The same code runs against AWS S3 in production and a local S3-compatible server in
development; only the settings differ.
"""

from __future__ import annotations

from dataclasses import dataclass

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import get_settings


class ObjectNotFound(LookupError):
    pass


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    content_type: str | None


class S3Store:
    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ):
        self.bucket = bucket
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            # A custom endpoint is addressed as host/bucket/key; AWS itself uses its default.
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"} if endpoint_url else {}),
        )

    def head(self, key: str) -> ObjectInfo | None:
        """Size and type of an object, or None when there is no such object."""
        try:
            found = self._s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return ObjectInfo(found["ContentLength"], found.get("ContentType"))

    def get(self, key: str) -> bytes:
        try:
            return self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                raise ObjectNotFound(key) from None
            raise

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=key)

    def presigned_url(self, key: str, *, expires_s: int = 900) -> str:
        """A link that lets its holder download one object for a short time."""
        return self._s3.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_s
        )

    def ensure_bucket(self) -> None:
        """Create the bucket if it is missing. For local development and tests."""
        try:
            self._s3.head_bucket(Bucket=self.bucket)
        except ClientError:
            self._s3.create_bucket(Bucket=self.bucket)


def get_store() -> S3Store:
    s = get_settings()
    return S3Store(
        s.s3_bucket,
        endpoint_url=s.s3_endpoint_url,
        region=s.s3_region,
        access_key_id=s.s3_access_key_id.get_secret_value() if s.s3_access_key_id else None,
        secret_access_key=s.s3_secret_access_key.get_secret_value() if s.s3_secret_access_key else None,
    )
