"""Offline, object-only verification of reviewed upstream source recipes."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from urllib.parse import urlsplit


class VerificationError(ValueError):
    """An input or reconstruction does not satisfy the reviewed recipe."""


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def fields(value, required, optional=()):
    require(isinstance(value, dict), "recipe objects must be tables")
    require(set(required) <= value.keys(), "missing fields: " + ", ".join(sorted(set(required) - value.keys())))
    require(value.keys() <= set(required) | set(optional),
            "unknown fields: " + ", ".join(sorted(value.keys() - set(required) - set(optional))))


def text(value):
    require(isinstance(value, str) and bool(value.strip()) and not any(ord(c) < 32 for c in value),
            "expected nonempty text without control characters")
    return value


def oid(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value),
            "object IDs must be full lowercase hexadecimal SHA-1 or SHA-256")
    return value


def digest(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "expected SHA-256 digest")
    return value


def url(value):
    text(value)
    parsed = urlsplit(value)
    require(parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment
            and not parsed.port and parsed.path.startswith("/") and not any(c in value for c in "\\ %"),
            "URLs must use canonical HTTPS without credentials, port, query or fragment")
    require(all(part not in (".", "..") for part in parsed.path.split("/")), "unsafe URL path")
    return value


def safe_path(value, empty=False):
    require(isinstance(value, str), "expected a relative POSIX path")
    if empty and value == "":
        return value
    text(value)
    path = PurePosixPath(value)
    require(value != "." and not path.is_absolute() and "\\" not in value and path.as_posix() == value
            and ":" not in value and all(p.lower() not in (".", "..", ".git") for p in path.parts), "unsafe relative path")
    return value


def ref(value, release=False):
    text(value)
    if release and not value.startswith("refs/"):
        value = "refs/tags/" + value
    require(value.startswith("refs/tags/") if release else
            value.startswith(("refs/tags/", "refs/remotes/upstream/")), "expected a canonical upstream ref")
    require(not any(c in value for c in " ~^:?*[\\") and ".." not in value and "@{" not in value
            and not value.endswith(("/", ".", ".lock")) and "//" not in value
            and all(not p.startswith(".") for p in value.split("/")), "invalid Git ref")
    return value


@dataclass(frozen=True)
class Topic:
    branch: str
    commit: str
    operation: str
    kind: str
    depends_on: tuple[str, ...]
    allowed_added_commits: frozenset[str]
    origin: dict | None
    adaptation: dict | None
    resolution: dict | None


@dataclass(frozen=True)
class Recipe:
    source_kind: str
    upstream_url: str
    fork_url: str
    baseline: str
    baseline_ref: str
    expected_tree: str
    topics: tuple[Topic, ...]
    projection: dict | None
    archive: dict | None


def parse_recipe(data):
    fields(data, ("schema_version", "source_kind", "upstream_url", "fork_url", "baseline", "baseline_ref",
                  "expected_tree", "topics"), ("review", "projection", "archive"))
    require(type(data["schema_version"]) is int and data["schema_version"] == 1, "unsupported schema_version")
    require(data["source_kind"] in ("release", "reviewed-snapshot", "maintained-fork"), "invalid source_kind")
    if data["source_kind"] != "release":
        require("review" in data, "non-release baseline requires review")
    if "review" in data:
        url(data["review"])
    require(isinstance(data["topics"], list), "topics must be an ordered array")
    topics, previous = [], set()
    for entry in data["topics"]:
        fields(entry, ("branch", "commit", "operation", "kind"),
               ("depends_on", "allowed_added_commits", "upstream_pr", "origin", "adaptation", "resolution"))
        branch = text(entry["branch"])
        require(branch not in previous, "duplicate topic branch")
        operation, kind = entry["operation"], entry["kind"]
        require(operation in ("merge", "cherry-pick"), "invalid topic operation")
        require(kind in ("contribution", "backport", "integration"), "invalid topic kind")
        dependencies = entry.get("depends_on", [])
        require(isinstance(dependencies, list) and all(isinstance(d, str) for d in dependencies),
                "depends_on must be an array of earlier branch labels")
        require(set(dependencies) <= previous and len(dependencies) == len(set(dependencies)),
                "dependencies must name distinct earlier topics")
        allowed = entry.get("allowed_added_commits", [])
        require(isinstance(allowed, list), "allowed_added_commits must be an explicit array")
        for commit in allowed:
            oid(commit)
        require(len(allowed) == len(set(allowed)), "duplicate allowed ancestry commit")
        require(operation != "merge" or ("allowed_added_commits" in entry and bool(allowed)),
                "merge requires a complete nonempty added-ancestry allowlist")
        require(operation != "cherry-pick" or "allowed_added_commits" not in entry,
                "cherry-pick selects a delta, not an ancestry allowlist")
        if "upstream_pr" in entry:
            url(entry["upstream_pr"])
        origin, adaptation, resolution = (entry.get(k) for k in ("origin", "adaptation", "resolution"))
        require(kind != "backport" or origin is not None, "backport requires canonical origin evidence")
        require(adaptation is None or origin is not None, "adaptation requires origin evidence")
        if origin is not None:
            fields(origin, ("commit", "ref", "ref_commit"))
            oid(origin["commit"])
            oid(origin["ref_commit"])
            ref(origin["ref"])
        if adaptation is not None:
            fields(adaptation, ("original_diff_sha256", "applied_diff_sha256", "reason", "review"))
            digest(adaptation["original_diff_sha256"])
            digest(adaptation["applied_diff_sha256"])
            text(adaptation["reason"])
            url(adaptation["review"])
        if resolution is not None:
            fields(resolution, ("onto_tree", "topic_tree", "result_tree", "reason", "review"))
            for key in ("onto_tree", "topic_tree", "result_tree"):
                oid(resolution[key])
            text(resolution["reason"])
            url(resolution["review"])
        require(operation != "merge" or origin is None or
                (origin["commit"] == entry["commit"] and adaptation is None),
                "merge origins must name the original topic; adapted backports use cherry-pick")
        topics.append(Topic(branch, oid(entry["commit"]), operation, kind, tuple(dependencies),
                            frozenset(allowed), origin, adaptation, resolution))
        previous.add(branch)
    projection = data.get("projection")
    if projection is not None:
        fields(projection, ("source_prefix", "consumed_prefix"))
        for value in projection.values():
            safe_path(value, empty=True)
    archive = data.get("archive")
    if archive is not None:
        fields(archive, ("sha256", "prefix"))
        digest(archive["sha256"])
        safe_path(archive["prefix"], empty=True)
    return Recipe(data["source_kind"], url(data["upstream_url"]), url(data["fork_url"]),
                  oid(data["baseline"]), ref(data["baseline_ref"], data["source_kind"] == "release"),
                  oid(data["expected_tree"]), tuple(topics), projection, archive)


def load_recipe(path):
    path = Path(path)
    if path.suffix == ".toml":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    elif path.suffix == ".json":
        def pairs(items):
            result = {}
            for key, value in items:
                require(key not in result, "duplicate JSON field")
                result[key] = value
            return result
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    else:
        raise VerificationError("recipe filename must end in .toml or .json")
    return parse_recipe(data)


def git_environment():
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_REPLACE_OBJECTS="1",
                       GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
                       GIT_AUTHOR_NAME="Source reconstruction", GIT_AUTHOR_EMAIL="source@example.invalid",
                       GIT_COMMITTER_NAME="Source reconstruction", GIT_COMMITTER_EMAIL="source@example.invalid",
                       GIT_AUTHOR_DATE="2000-01-01T00:00:00Z", GIT_COMMITTER_DATE="2000-01-01T00:00:00Z",
                       LC_ALL="C")
    return environment


class Git:
    def __init__(self, repository, environment=None):
        self.repository = Path(repository).resolve(strict=True)
        self.environment = environment or git_environment()

    def run(self, *args, input=None, check=True):
        command = ["git", "--no-replace-objects", "-C", str(self.repository), "-c", "core.hooksPath=" + os.devnull,
                   "-c", "protocol.allow=never", "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false",
                   *map(str, args)]
        result = subprocess.run(command, input=input, capture_output=True, env=self.environment)
        if check and result.returncode:
            raise VerificationError("git " + str(args[0]) + " failed; required objects or clean reconstruction unavailable")
        return result

    def out(self, *args, **kwargs):
        return self.run(*args, **kwargs).stdout.decode("utf-8").strip()

    def object(self, value, kind):
        require(self.out("cat-file", "-t", oid(value)) == kind, f"expected {kind} object")
        return value

    def tree(self, commit):
        return self.out("rev-parse", "--verify", commit + "^{tree}")

    def parents(self, commit):
        return self.out("show", "-s", "--format=%P", commit).split()

    def delta(self, before, after):
        return self.run("diff", "--binary", "--full-index", "--no-renames", "--no-ext-diff", "--no-textconv",
                        before, after, "--").stdout

    def commit(self, tree, parents, author=None):
        args = ["commit-tree", tree]
        for parent in parents:
            args += ["-p", parent]
        environment = self.environment
        if author:
            name, email, date = self.out("show", "-s", "--format=%an%n%ae%n%aI", author).splitlines()
            environment = {**environment, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date}
        return Git(self.repository, environment).out(*args, input=b"Offline source reconstruction\n")


def exact_ref(repository, objects, name, expected):
    raw = repository.out("show-ref", "--verify", "--hash", ref(name))
    actual = objects.out("rev-parse", "--verify", raw + "^{commit}")
    require(actual == expected, "canonical ref moved or does not match its recorded commit: " + name)


def source_objects(repository, recipe):
    # release_review disables grafts for its children with this impossible path.
    # Accept only that sentinel; Git reads below still use a sanitized environment.
    graft_file = os.environ.get("GIT_GRAFT_FILE")
    require(not graft_file or graft_file == os.path.join(os.devnull, "no-grafts"),
            "history grafts are not accepted")
    require(repository.out("rev-parse", "--is-shallow-repository") == "false", "shallow history cannot prove ancestry")
    grafts = Path(repository.out("rev-parse", "--path-format=absolute", "--git-path", "info/grafts"))
    require(not grafts.exists(), "history grafts are not accepted")
    for remote, expected in (("upstream", recipe.upstream_url), ("origin", recipe.fork_url)):
        require(repository.out("config", "--get-all", "remote." + remote + ".url") == expected,
                remote + " URL does not match the reviewed recipe")
    return Path(repository.out("rev-parse", "--path-format=absolute", "--git-path", "objects"))


def single_delta(repository, commit):
    parents = repository.parents(commit)
    require(len(parents) == 1, "delta evidence requires a commit with exactly one parent")
    return repository.delta(parents[0], commit)


def replay_topic(repository, current, topic):
    onto_tree, topic_tree = repository.tree(current), repository.tree(topic.commit)
    if topic.origin:
        result = repository.run("merge-base", "--is-ancestor", topic.origin["commit"], topic.origin["ref_commit"], check=False)
        require(result.returncode == 0, "backport origin is absent from recorded canonical upstream history")
    if topic.operation == "merge":
        added = set(repository.out("rev-list", topic.commit, "^" + current, "--").split())
        require(added == topic.allowed_added_commits, "merge adds undeclared ancestry or has a stale ancestry allowlist")
        result = repository.run("merge-base", "--is-ancestor", repository.baseline, topic.commit, check=False)
        require(result.returncode == 0, "merge topic must descend from the selected baseline")
        if topic.origin:
            require(all(repository.run("merge-base", "--is-ancestor", commit,
                                       topic.origin["ref_commit"], check=False).returncode == 0 for commit in added),
                    "merge backport ancestry is absent from canonical upstream history")
        result = repository.run("merge-tree", "--write-tree", current, topic.commit, check=False)
        require(result.returncode in (0, 1), "merge-tree failed; complete inputs or Git >=2.38 are required")
        require(bool(result.stdout.splitlines()), "merge-tree did not return a result tree")
        candidate = oid(result.stdout.splitlines()[0].decode("ascii"))
        clean = result.returncode == 0
        parents = [current, topic.commit]
    else:
        patch = single_delta(repository, topic.commit)
        require(bool(patch), "empty cherry-pick input is not a source change")
        repository.run("read-tree", onto_tree)
        result = repository.run("apply", "--cached", "--binary", "--whitespace=nowarn", "-", input=patch, check=False)
        clean = result.returncode == 0
        candidate = repository.out("write-tree") if clean else None
        parents = [current]
    if topic.resolution:
        require(not clean, "resolution evidence is stale; the operation no longer conflicts")
        resolution = topic.resolution
        require(resolution["onto_tree"] == onto_tree and resolution["topic_tree"] == topic_tree,
                "reviewed conflict resolution inputs changed")
        candidate = repository.object(resolution["result_tree"], "tree")
    else:
        require(clean, "unresolved operation; review and declare the exact conflict resolution inputs")
    repository.object(candidate, "tree")
    if topic.origin and topic.operation == "cherry-pick":
        original = single_delta(repository, topic.origin["commit"])
        applied = repository.delta(current, candidate)
        if topic.adaptation:
            require(hashlib.sha256(original).hexdigest() == topic.adaptation["original_diff_sha256"]
                    and hashlib.sha256(applied).hexdigest() == topic.adaptation["applied_diff_sha256"],
                    "adaptation evidence does not match the original and applied deltas")
        else:
            require(original == applied, "backport delta differs from origin; reviewed adaptation evidence is required")
    return repository.commit(candidate, parents, topic.commit if topic.operation == "cherry-pick" else None)


def subtree(repository, tree, prefix):
    if not prefix:
        return tree
    value = repository.out("rev-parse", "--verify", tree + ":" + prefix)
    return repository.object(value, "tree")


def entries(repository, tree):
    result = {}
    for row in repository.run("ls-tree", "-rz", "--full-tree", tree).stdout.split(b"\0"):
        if not row:
            continue
        header, raw_path = row.split(b"\t", 1)
        mode, kind, blob = header.decode("ascii").split()
        name = safe_path(raw_path.decode("utf-8"))
        require(kind == "blob" and mode in ("100644", "100755", "120000"), "unsupported tree entry")
        result[name] = (mode, repository.run("cat-file", "blob", blob).stdout)
    return result


def verify_archive(path, specification, expected):
    require(hashlib.sha256(Path(path).read_bytes()).hexdigest() == specification["sha256"], "archive checksum differs")
    prefix = specification["prefix"]
    found = {}
    paths = {}
    with tarfile.open(path, "r:*") as archive:
        for member in archive:
            name = member.name.removesuffix("/")
            safe_path(name)
            # Extraction also creates implicit parent directories. A file or
            # symlink cannot occupy any of those paths, in either member order.
            parts = name.split("/")
            for end in range(1, len(parts)):
                parent = "/".join(parts[:end])
                require(paths.get(parent, "directory") == "directory", "archive path type collision")
                paths[parent] = "directory"
            kind = "directory" if member.isdir() else "file"
            require(name not in paths or (paths[name] == kind == "directory"), "archive path type collision or duplicate member")
            paths[name] = kind
            require(not member.mode & 0o7000, "archive special mode bits are not accepted")
            if prefix:
                require(name == prefix or name.startswith(prefix + "/"), "archive member outside declared prefix")
                name = name.removeprefix(prefix).removeprefix("/")
            if member.isdir():
                continue
            safe_path(name)
            require(name not in found, "duplicate archive member")
            if member.issym():
                safe_path(member.linkname)
                value = ("120000", member.linkname.encode("utf-8"))
            else:
                require(member.isfile(), "archive supports regular files and symlinks only")
                handle = archive.extractfile(member)
                require(handle is not None, "missing archive member content")
                with handle:
                    value = ("100755" if member.mode & 0o100 else "100644", handle.read())
            found[name] = value
    require(found == expected, "archive source content or executable modes differ from the verified tree")


def verify(repository, recipe_path, consumed_ref, archive=None):
    recipe = load_recipe(recipe_path)
    consumed = oid(consumed_ref)
    source = Git(repository)
    objects = source_objects(source, recipe)
    version = source.out("--version")
    numbers = re.search(r"(\d+)\.(\d+)", version)
    require(numbers is not None and tuple(map(int, numbers.groups())) >= (2, 38), "Git >=2.38 is required")
    require((archive is not None) == (recipe.archive is not None), "archive argument and recipe archive evidence must both be present")
    with tempfile.TemporaryDirectory(prefix="upstream-source-") as directory:
        root = Path(directory)
        environment = git_environment()
        object_format = source.out("rev-parse", "--show-object-format")
        result = subprocess.run(["git", "init", "--bare", "--object-format=" + object_format, str(root)],
                                capture_output=True, env=environment)
        require(result.returncode == 0, "cannot initialize disposable object store")
        # The alternate is read-only; generated objects and the temporary index stay here.
        object_path = objects.as_posix()
        require(not any(c in object_path for c in '\n\r"\\'), "unsafe object-store path")
        (root / "objects/info/alternates").write_text(object_path + "\n", encoding="utf-8")
        work = Git(root, environment)
        exact_ref(source, work, recipe.baseline_ref, recipe.baseline)
        work.object(recipe.baseline, "commit")
        work.object(consumed, "commit")
        for topic in recipe.topics:
            work.object(topic.commit, "commit")
            if topic.origin:
                exact_ref(source, work, topic.origin["ref"], topic.origin["ref_commit"])
                work.object(topic.origin["commit"], "commit")
        work.baseline = recipe.baseline
        required = [recipe.baseline, consumed]
        for topic in recipe.topics:
            required.append(topic.commit)
            if topic.origin:
                required += [topic.origin["commit"], topic.origin["ref_commit"]]
        work.run("rev-list", "--objects", "--missing=error", *dict.fromkeys(required), "--")
        current = recipe.baseline
        for topic in recipe.topics:
            current = replay_topic(work, current, topic)
        tree = work.tree(current)
        require(tree == recipe.expected_tree, "reconstructed tree differs from expected_tree")
        source_prefix = recipe.projection["source_prefix"] if recipe.projection else ""
        consumed_prefix = recipe.projection["consumed_prefix"] if recipe.projection else ""
        selected = subtree(work, tree, source_prefix)
        actual = subtree(work, work.tree(consumed), consumed_prefix)
        require(selected == actual, "consumed source tree differs from the reconstructed tree")
        if archive is not None:
            verify_archive(archive, recipe.archive, entries(work, selected))
    return {"status": "verified", "schema_version": 1, "baseline": recipe.baseline,
            "tree": tree, "consumed_ref": consumed, "consumed_tree": actual, "topics": len(recipe.topics),
            "source_kind": recipe.source_kind}


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(64, f"{self.prog}: error: {message}\n")


def main(argv=None):
    parser = ArgumentParser(description=__doc__, epilog="Exit status: 0 verified; 1 refused; 64 invalid arguments.")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("verify", help="reconstruct acquired objects offline and compare the consumed tree",
                           epilog="Exit status: 0 verified; 1 refused; 64 invalid arguments.")
    check.add_argument("--repository", required=True, type=Path)
    check.add_argument("--recipe", required=True, type=Path)
    check.add_argument("--consumed-ref", required=True)
    check.add_argument("--archive", type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify(args.repository, args.recipe, args.consumed_ref, args.archive)
    except (VerificationError, OSError, ValueError, tarfile.TarError, UnicodeError) as error:
        print("upstream-source: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
