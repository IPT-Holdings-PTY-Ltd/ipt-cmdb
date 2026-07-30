import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from src.cmdb.version import APPLICATION_VERSION, release_metadata

ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    def test_checked_in_version_is_the_node_release_version(self):
        version_file = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        package_version = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]

        self.assertEqual(APPLICATION_VERSION, version_file)
        self.assertEqual(APPLICATION_VERSION, package_version)

    def test_valid_runtime_metadata_is_normalized(self):
        commit = "A" * 40
        digest = "sha256:" + ("B" * 64)
        with patch.dict(
            os.environ,
            {
                "CMDB_VERSION": "0.4.0-rc.1",
                "CMDB_SOURCE_COMMIT": commit,
                "CMDB_IMAGE_DIGEST": digest,
            },
        ):
            metadata = release_metadata()

        self.assertEqual(metadata.application_version, "0.4.0-rc.1")
        self.assertEqual(metadata.source_commit, commit.lower())
        self.assertEqual(metadata.image_digest, digest.lower())
        self.assertEqual(
            metadata.public(),
            {
                "applicationVersion": "0.4.0-rc.1",
                "sourceCommit": commit.lower(),
                "imageDigest": digest.lower(),
            },
        )

    def test_invalid_runtime_metadata_never_reaches_public_output(self):
        secret = "password=do-not-publish"
        with patch.dict(
            os.environ,
            {
                "CMDB_VERSION": secret,
                "CMDB_SOURCE_COMMIT": secret,
                "CMDB_IMAGE_DIGEST": secret,
            },
        ):
            public = release_metadata().public()

        self.assertEqual(public["applicationVersion"], APPLICATION_VERSION)
        self.assertEqual(public["sourceCommit"], "unknown")
        self.assertEqual(public["imageDigest"], "unknown")
        self.assertNotIn(secret, json.dumps(public))

    def test_container_and_deployment_contracts_forward_release_metadata(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        build_default = re.search(r"^ARG CMDB_VERSION=(.+)$", dockerfile, re.MULTILINE)
        self.assertIsNotNone(build_default)
        self.assertEqual(build_default.group(1), APPLICATION_VERSION)
        self.assertIn("COPY VERSION ./", dockerfile)
        self.assertIn("org.opencontainers.image.version", dockerfile)
        self.assertIn("org.opencontainers.image.revision", dockerfile)

        for relative_path in (
            "docker-compose.yml",
            "compose.production.yml",
            "compose.appliance.yml",
            "compose.worker.yml",
        ):
            compose = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn("CMDB_IMAGE_DIGEST:", compose, relative_path)

        bicep = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
        self.assertGreaterEqual(bicep.count("name: 'CMDB_IMAGE_DIGEST'"), 2)


if __name__ == "__main__":
    unittest.main()
