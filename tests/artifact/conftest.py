"""Fixtures for the artifact-storage tests: an S3 server in this process (plan 09, Task 9).

The S3 store is tested against moto's threaded server on 127.0.0.1 (``moto[s3]``, ``flask`` and ``flask-cors`` in the dev group), never against a
real AWS endpoint: no test needs network access. The server and its bucket are made once per session; a test that writes uses a key prefix of its
own. Both fixtures skip when the ``s3`` extra (aiobotocore) or moto is not installed, so the S3 tests skip on a core install; in the dev and CI
environments both are installed and the tests must pass, not skip.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

#: The bucket the session's S3 server holds.
S3_BUCKET = "primer-artifacts"
#: The keys the test rows carry. The server accepts any.
S3_TEST_KEY = "test"
S3_TEST_SECRET = "test"

#: Every environment variable through which botocore could find credentials, a profile, a region or an endpoint on the machine running the tests.
_AWS_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_DEFAULT_REGION",
    "AWS_REGION",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT_URL_S3",
)


@pytest.fixture
def isolated_aws_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No credentials, profile or region from the machine running the tests, and no instance-metadata lookup (which would reach the network).

    botocore's ambient chain reads the environment, the shared credentials and config files, and the EC2 instance metadata service. A test that
    wants ambient credentials sets them in the environment itself.
    """
    for name in _AWS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))


@pytest.fixture(scope="session")
def s3_endpoint() -> Iterator[str]:
    """The URL of moto's threaded S3 server on 127.0.0.1, holding the bucket :data:`S3_BUCKET`."""
    pytest.importorskip("aiobotocore")
    moto_server = pytest.importorskip("moto.server")
    boto3 = pytest.importorskip("boto3")
    server = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    try:
        _, port = server.get_host_and_port()
        endpoint = f"http://127.0.0.1:{port}"
        boto3.client(
            "s3", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id=S3_TEST_KEY, aws_secret_access_key=S3_TEST_SECRET,
        ).create_bucket(Bucket=S3_BUCKET)
        yield endpoint
    finally:
        server.stop()


@pytest.fixture
def s3_prefix() -> str:
    """A key prefix of this test's own, so tests sharing the session's bucket never see each other's objects."""
    return f"t-{uuid4().hex[:8]}/"


@pytest.fixture
def s3_admin(s3_endpoint: str):
    """A plain synchronous boto3 client on the session's server, for a test to look at the bucket directly."""
    import boto3

    return boto3.client(
        "s3", endpoint_url=s3_endpoint, region_name="us-east-1", aws_access_key_id=S3_TEST_KEY, aws_secret_access_key=S3_TEST_SECRET,
    )


def s3_keys_under(s3_admin, prefix: str) -> list[str]:
    """Every object key in the session's bucket that starts with ``prefix``."""
    listed = s3_admin.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
    return sorted(item["Key"] for item in listed.get("Contents", []))
