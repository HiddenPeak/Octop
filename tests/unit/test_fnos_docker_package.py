"""Verify the Docker image reference shipped in the built FnOS payload."""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
posix_only = pytest.mark.skipif(os.name != "posix", reason="FPK builder requires POSIX shell tools")


@posix_only
@pytest.mark.parametrize(
    ("image_env", "origin", "expected_image"),
    [
        (
            {
                "FPK_IMAGE_REPOSITORY": "ghcr.io/rminte/octop",
                "GITHUB_REPOSITORY": "OtherOwner/OtherRepo",
            },
            "git@github.com:AnotherOwner/AnotherRepo.git",
            "ghcr.io/rminte/octop:9.8.7",
        ),
        (
            {"GITHUB_REPOSITORY": "ExampleOwner/ExampleRepo"},
            "git@github.com:RMinte/Octop.git",
            "ghcr.io/exampleowner/examplerepo:9.8.7",
        ),
        (
            {"FPK_IMAGE_TAG": "latest"},
            "git@github.com:RMinte/Octop.git",
            "ghcr.io/rminte/octop:latest",
        ),
        (
            {},
            "https://github.com/LocalOwner/LocalRepo.git",
            "ghcr.io/localowner/localrepo:9.8.7",
        ),
        ({}, None, "ghcr.io/rminte/octop:9.8.7"),
    ],
    ids=[
        "ci-image-output",
        "github-repository",
        "ssh-origin-latest",
        "https-origin",
        "source-archive",
    ],
)
def test_built_docker_payload_uses_publishing_repository(
    tmp_path: Path, image_env: dict[str, str], origin: str | None, expected_image: str
):
    """Release and local packages must follow the repository that publishes their image."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(ROOT / "scripts" / "build-fpk.sh", scripts / "build-fpk.sh")
    package = tmp_path / "fnos" / "docker"
    shutil.copytree(ROOT / "fnos" / "docker", package)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "9.8.7"\n', encoding="utf-8")

    # Replace only the external packager; run the repository's staging and
    # version injection before inspecting the payload that it handed off.
    verify = tmp_path / ".verify"
    verify.mkdir()
    fnpack = verify / "fnpack"
    fnpack.write_text(
        "#!/bin/sh\nset -eu\ntar -czf inhere-box.fpk manifest app\n", encoding="utf-8"
    )
    fnpack.chmod(0o755)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("FPK_") and key != "GITHUB_REPOSITORY"
    }
    env.update(image_env)
    if origin is not None:
        subprocess.run(
            ["git", "init", str(tmp_path)], env=env, check=True, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "remote", "add", "origin", origin],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    subprocess.run(
        ["bash", str(scripts / "build-fpk.sh"), "docker"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    archive = tmp_path / "dist" / "inhere-box-docker-9.8.7.fpk"
    with tarfile.open(archive, "r:gz") as bundle:
        compose_file = bundle.extractfile("app/docker/docker-compose.yaml")
        assert compose_file is not None
        compose = yaml.safe_load(compose_file.read())
    assert compose["services"]["inhere-box"]["image"] == expected_image


def test_docker_fpk_workflow_uses_the_published_image_output():
    workflow = yaml.safe_load((ROOT / ".github/workflows/fnos-build-fpk.yml").read_text("utf-8"))
    package_step = next(
        step
        for step in workflow["jobs"]["fpk"]["steps"]
        if step.get("run") == "bash scripts/build-fpk.sh docker"
    )
    assert package_step["env"]["FPK_IMAGE_REPOSITORY"] == "${{ needs.version.outputs.image }}"
