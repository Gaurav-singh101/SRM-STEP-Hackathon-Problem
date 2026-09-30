"""Resolve image tags to immutable digests (CLAUDE.md §11).

    uv run python -m tests.tools.pin_digests docker-compose.yml
    uv run python -m tests.tools.pin_digests docker-compose.yml --check

A tag is mutable. `postgres:16-alpine` today and the same string in three months can be
different bytes, which means "the image we tested" and "the image running" are not
necessarily the same thing — and that gap is invisible in a diff. A digest closes it.

Queries the registry over HTTPS rather than shelling out to `docker manifest inspect`, so it
works without a running Docker daemon. Anonymous pull tokens are enough for public images; no
credential is read or needed.

``--check`` exits non-zero when any image is still tag-only, which is what makes this usable
as a CI gate rather than a one-off script someone forgets to run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

#: Matches `image: name:tag` and captures the parts, leaving any existing @sha256 alone.
IMAGE_LINE = re.compile(
    r"^(?P<indent>\s*)image:\s*(?P<name>[^\s:@]+):(?P<tag>[^\s@]+)(?P<digest>@sha256:[0-9a-f]{64})?\s*$"
)

DOCKER_AUTH = "https://auth.docker.io/token"
DOCKER_REGISTRY = "https://registry-1.docker.io"

#: Both are needed. A multi-arch image returns an index under the first type and a single
#: manifest under the second, and asking for only one gets a 404 on half of real images.
ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


@dataclass(frozen=True, slots=True)
class Image:
    name: str
    tag: str
    digest: str | None

    @property
    def repository(self) -> str:
        """Docker Hub official images live under `library/`, which the short name hides."""
        return self.name if "/" in self.name else f"library/{self.name}"

    @property
    def pinned(self) -> bool:
        return self.digest is not None


def _token(repository: str) -> str:
    url = f"{DOCKER_AUTH}?service=registry.docker.io&scope=repository:{repository}:pull"
    with urllib.request.urlopen(url, timeout=20) as response:
        return str(json.load(response)["token"])


def resolve(image: Image) -> str:
    """The digest the registry currently serves for that tag.

    Read from the `Docker-Content-Digest` header rather than hashing the body: the header is
    what the registry itself considers canonical, and recomputing it locally would be a second
    opinion that can disagree.
    """
    token = _token(image.repository)
    url = f"{DOCKER_REGISTRY}/v2/{image.repository}/manifests/{image.tag}"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": ACCEPT},
        method="HEAD",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        digest = response.headers.get("Docker-Content-Digest")
    if not digest:
        raise RuntimeError(f"registry returned no digest for {image.name}:{image.tag}")
    return str(digest)


def parse(path: Path) -> list[tuple[int, Image]]:
    found = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        match = IMAGE_LINE.match(line)
        if match:
            found.append(
                (
                    number,
                    Image(
                        name=match.group("name"),
                        tag=match.group("tag"),
                        digest=match.group("digest"),
                    ),
                )
            )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pin_digests", description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report unpinned images and exit non-zero; write nothing",
    )
    args = parser.parse_args(argv)

    if not args.file.exists():
        print(f"{args.file} does not exist", file=sys.stderr)
        return 2

    images = parse(args.file)
    if not images:
        print(f"no image references found in {args.file}")
        return 0

    unpinned = [(n, i) for n, i in images if not i.pinned]

    print(f"{len(images)} image reference(s), {len(unpinned)} unpinned")
    for _, image in images:
        state = "pinned" if image.pinned else "TAG ONLY"
        print(f"  [{state}] {image.name}:{image.tag}")

    if args.check:
        if unpinned:
            print(
                f"\n{len(unpinned)} image(s) are pinned by tag only. A tag is mutable, so the "
                "image that was tested and the image that runs may differ (CLAUDE.md S11).",
                file=sys.stderr,
            )
            return 1
        print("\nevery image is pinned by digest")
        return 0

    lines = args.file.read_text(encoding="utf-8").splitlines(keepends=True)
    rewritten = 0
    for number, image in unpinned:
        try:
            digest = resolve(image)
        except (urllib.error.URLError, RuntimeError, KeyError) as exc:
            # Reported per image rather than fatal: one unreachable repository should not stop
            # the others from being pinned.
            print(f"  could not resolve {image.name}:{image.tag}: {exc}", file=sys.stderr)
            continue
        original = lines[number]
        lines[number] = original.replace(
            f"{image.name}:{image.tag}", f"{image.name}:{image.tag}@{digest}", 1
        )
        print(f"  pinned {image.name}:{image.tag} -> {digest}")
        rewritten += 1

    if rewritten:
        args.file.write_text("".join(lines), encoding="utf-8")
        print(f"\nrewrote {rewritten} reference(s) in {args.file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
