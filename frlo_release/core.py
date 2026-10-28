from __future__ import annotations

import hashlib
import gzip
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath

def find_root():
    if getattr(sys, "frozen", False):
        for candidate in Path(sys.executable).resolve().parents:
            if (candidate / "config/project.json").is_file() and (candidate / "release").is_dir():
                return candidate
        raise RuntimeError("Place the GUI inside the FrLO publishing repository")
    return Path(__file__).resolve().parents[1]


ROOT = find_root()
USER_AGENT = "Arcomua-FrLO-release/1.0 (github.com/Arcomua/FrLO)"
LOADERS = {"fabric-loader": "fabric", "quilt-loader": "quilt", "forge": "forge", "neoforge": "neoforge"}
DOWNLOAD_HOSTS = {"cdn.modrinth.com", "mediafilez.forgecdn.net", "edge.forgecdn.net", "media.forgecdn.net"}
MAX_ARCHIVE_BYTES = 2 * 1024**3


class ReleaseError(RuntimeError):
    pass


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def request_bytes(url, *, method="GET", data=None, headers=None):
    """Retry reads through the user's proxy. Never retry an ambiguous write."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ReleaseError("Network requests must use HTTPS")
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"User-Agent": USER_AGENT, **(headers or {})})
    # Redirects must not carry credentials to another host.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            raise ReleaseError("Unexpected API/download redirect; update the configured source")
    handlers = [urllib.request.ProxyHandler(), urllib.request.ProxyHandler({"https": "http://127.0.0.1:10808"})]
    for i, proxy in enumerate(handlers):
        try:
            with urllib.request.build_opener(proxy, NoRedirect()).open(request, timeout=90) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            # Do not print server responses which could echo credentials.
            raise ReleaseError(f"{urllib.parse.urlsplit(url).hostname}: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if method != "GET" or i:
                raise ReleaseError(f"{urllib.parse.urlsplit(url).hostname}: network failure; check connectivity. "
                                   "If this was an upload, inspect the platform before retrying.") from None


def http_json(url, method="GET", data=None, headers=None):
    actual_headers = dict(headers or {})
    if isinstance(data, (dict, list)):
        data = json.dumps(data).encode("utf-8")
        actual_headers["Content-Type"] = "application/json"
    return json.loads(request_bytes(url, method=method, data=data, headers=actual_headers))


def safe_path(text):
    if not isinstance(text, str) or not text or "\\" in text or ":" in text or "\x00" in text:
        raise ReleaseError(f"Invalid archive path: {text!r}")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in text.rstrip("/").split("/")):
        raise ReleaseError(f"Unsafe archive path: {text!r}")
    if any(part.endswith((" ", ".")) or part.split(".")[0].upper() in
           {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
           for part in path.parts):
        raise ReleaseError(f"Nonportable archive path: {text!r}")
    return path


def digest(data, algorithm="sha512"):
    return hashlib.new(algorithm, data).hexdigest()


def validate_index(index):
    if index.get("formatVersion") != 1 or index.get("game") != "minecraft":
        raise ReleaseError("Only Modrinth Minecraft pack formatVersion=1 is supported")
    dependencies = index.get("dependencies", {})
    if not dependencies.get("minecraft") or len(set(dependencies) & LOADERS.keys()) != 1:
        raise ReleaseError("The pack must declare Minecraft and exactly one supported loader")
    paths = set()
    for entry in index.get("files", []):
        path = str(safe_path(entry["path"]))
        if path.casefold() in paths:
            raise ReleaseError(f"Duplicate index path: {path}")
        paths.add(path.casefold())
        for algo, length in (("sha1", 40), ("sha512", 128)):
            if not re.fullmatch(f"[0-9a-fA-F]{{{length}}}", entry.get("hashes", {}).get(algo, "")):
                raise ReleaseError(f"Missing/invalid {algo}: {path}")
        if not isinstance(entry.get("fileSize"), int) or not 0 < entry["fileSize"] <= MAX_ARCHIVE_BYTES:
            raise ReleaseError(f"Invalid file size: {path}")
        urls = entry.get("downloads", [])
        if not urls or any(urllib.parse.urlsplit(url).scheme != "https" or
                           urllib.parse.urlsplit(url).hostname not in DOWNLOAD_HOSTS or
                           urllib.parse.urlsplit(url).username for url in urls):
            raise ReleaseError(f"Untrusted download host: {path}")
        for side in entry.get("env", {}).values():
            if side not in {"required", "optional", "unsupported"}:
                raise ReleaseError(f"Invalid environment: {path}")
    return index


def private_path(path):
    parts = tuple(p.casefold() for p in PurePosixPath(path).parts)
    if any(p in {".git", ".frlo-backups", ".local-backups", "logs", "crash-reports", "screenshots",
                 "backups", "playerdata", "advancements", "stats", "__pycache__"} for p in parts):
        # Data-pack advancements are content, not player progress.
        if "advancements" in parts and "data" in parts and "playerdata" not in parts and "stats" not in parts:
            return False
        return True
    name = parts[-1]
    return name in {"servers.dat", "servers.dat_old", "usercache.json", "usernamecache.json", "session.lock",
                    "accounts.json", "launcher_accounts.json", "realms_persistence.json", "optionsof.txt"} or \
        name.endswith((".log", ".bak", ".old", ".disabled", ".lck")) or name.startswith((".env", ".ds_store"))


def sanitize_options(data):
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    text = data[len(bom):].decode("utf-8", errors="surrogateescape")
    output = []
    for line in text.splitlines(keepends=True):
        ending = "\r\n" if line.endswith("\r\n") else line[-1:] if line.endswith(("\n", "\r")) else ""
        content = line[:-len(ending)] if ending else line
        if content.startswith("lastServer:"):
            content = "lastServer: "
        elif content.startswith("lang:"):
            content = "lang:en_us"
        output.append(content + ending)
    return bom + "".join(output).encode("utf-8", errors="surrogateescape")


def inventory(pack):
    if not (pack / "modrinth.index.json").exists():
        return {}
    index = read_json(pack / "modrinth.index.json")
    result = {}
    for entry in index.get("files", []):
        url = entry["downloads"][0]
        match = re.search(r"/data/([^/]+)/versions/", url)
        identity = f"modrinth:{match[1]}" if match else entry["path"]
        result[identity] = {"path": entry["path"], "sha512": entry["hashes"]["sha512"]}
    for path in sorted((pack / "overrides").rglob("*")):
        if path.is_file():
            relative = path.relative_to(pack / "overrides").as_posix()
            identity = relative
            if relative.startswith("mods/") and path.suffix == ".jar":
                try:
                    with zipfile.ZipFile(path) as jar:
                        if "fabric.mod.json" in jar.namelist():
                            identity = "embedded:" + json.loads(jar.read("fabric.mod.json"))["id"]
                except (ValueError, KeyError, zipfile.BadZipFile):
                    pass
            result[identity] = {"path": relative, "sha512": digest(path.read_bytes())}
    return result


def import_pack(source: Path, root=ROOT, log=print):
    source = source.resolve()
    root = Path(root).resolve()
    old = inventory(root / "pack")
    removed = []
    with tempfile.TemporaryDirectory(prefix="frlo-import-") as temporary:
        stage = Path(temporary) / "pack"
        stage.mkdir()
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES or len(infos) > 100000:
                raise ReleaseError("Archive exceeds import size/count limit")
            seen = set()
            for info in infos:
                path = safe_path(info.filename)
                if str(path).casefold() in seen:
                    raise ReleaseError(f"Duplicate archive member: {path}")
                seen.add(str(path).casefold())
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ReleaseError(f"Archive contains symlink: {path}")
                if path.parts[0] not in {"modrinth.index.json", "overrides", "client-overrides", "server-overrides"}:
                    raise ReleaseError(f"Unexpected archive root: {path}")
            index = validate_index(json.loads(archive.read("modrinth.index.json")))
            for info in infos:
                path = safe_path(info.filename)
                if info.is_dir() or str(path) == "modrinth.index.json":
                    continue
                if path.parts[0] in {"client-overrides", "server-overrides"}:
                    raise ReleaseError("Side-specific overrides require an explicit merge before importing; no files were changed")
                relative = PurePosixPath(*path.parts[1:]).as_posix()
                # Root options are local settings; config defaults must come from YOSBR.
                if private_path(relative) or relative == "options.txt" or (relative.startswith("config/") and
                                                                             not relative.startswith("config/yosbr/")):
                    removed.append(str(path))
                    continue
                data = archive.read(info)
                if relative == "config/yosbr/options.txt":
                    data = sanitize_options(data)
                # Worlds carry NBT player state even after playerdata is removed.
                if path.name in {"level.dat", "level.dat_old"}:
                    from .world import sanitize_level
                    data = sanitize_level(data)
                destination = stage.joinpath(*path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            kept = []
            for entry in index["files"]:
                if private_path(entry["path"]) or entry["path"] == "options.txt" or (
                    entry["path"].startswith("config/") and not entry["path"].startswith("config/yosbr/")):
                    removed.append(entry["path"])
                else:
                    if entry["path"].endswith(("level.dat", "level.dat_old")):
                        raise ReleaseError("World level.dat must be embedded so its player state can be sanitized")
                    kept.append(entry)
            index["files"] = sorted(kept, key=lambda f: f["path"])
            write_json(stage / "modrinth.index.json", index)
        # Validate collisions before replacing any existing source.
        collect_records(stage)
        new = inventory(stage)
        previous = root / "pack"
        backup = root / ".cache" / "previous-pack"
        if backup.exists():
            shutil.rmtree(backup)
        backup.parent.mkdir(parents=True, exist_ok=True)
        if previous.exists():
            previous.rename(backup)
        try:
            shutil.copytree(stage, previous)
        except BaseException:
            if previous.exists():
                shutil.rmtree(previous)
            if backup.exists():
                backup.rename(previous)
            raise
    sections = ["# 自动生成的文件变化", ""]
    for title, keys in (("新增", new.keys() - old.keys()), ("移除", old.keys() - new.keys()),
                        ("更新", {key for key in old.keys() & new.keys() if old[key] != new[key]})):
        sections.extend([f"## {title}", ""])
        sections.extend(f"- `{(new if key in new else old)[key]['path']}`" for key in sorted(keys))
        if not keys:
            sections.append("无")
        sections.append("")
    (root / "release").mkdir(exist_ok=True)
    (root / "release/changes.md").write_text("\n".join(sections), encoding="utf-8")
    write_json(root / "release/release.json", {"version": index.get("versionId", ""), "channel": "beta"})
    write_json(root / "release/import-report.json", {"removed": removed, "files": len(new), "source": source.name})
    log(f"Imported {len(new)} files; excluded {len(removed)} private/runtime/config paths. Report: release/import-report.json")
    return index


def collect_records(pack):
    if pack.is_symlink() or (pack / "overrides").is_symlink():
        raise ReleaseError("Pack roots cannot be symlinks")
    index = validate_index(read_json(pack / "modrinth.index.json"))
    for entry in index["files"]:
        path = entry["path"]
        if private_path(path) or path == "options.txt" or (path.startswith("config/") and not path.startswith("config/yosbr/")):
            raise ReleaseError(f"Private/runtime/config index path: {path}; reimport the pack")
        if PurePosixPath(path).name in {"level.dat", "level.dat_old"} or path == "config/yosbr/options.txt":
            raise ReleaseError(f"State-sensitive file must be embedded for sanitization: {path}")
    records = [{"path": e["path"], "sha512": e["hashes"]["sha512"].lower(),
                "sha1": e["hashes"]["sha1"].lower(), "size": e["fileSize"],
                "downloads": e["downloads"], "env": e.get("env", {})} for e in index["files"]]
    paths = {r["path"].casefold() for r in records}
    for path in sorted((pack / "overrides").rglob("*")):
        if path.is_symlink():
            raise ReleaseError(f"Symlink in pack: {path.name}")
        if not path.is_file():
            continue
        relative = path.relative_to(pack / "overrides").as_posix()
        safe_path(relative)
        if relative.casefold() in paths:
            raise ReleaseError(f"Indexed/embedded file collision: {relative}")
        if private_path(relative) or relative == "options.txt" or (relative.startswith("config/") and not relative.startswith("config/yosbr/")):
            raise ReleaseError(f"Private/runtime file in source: {relative}; reimport before building")
        paths.add(relative.casefold())
        data = path.read_bytes()
        if relative == "config/yosbr/options.txt" and sanitize_options(data) != data:
            raise ReleaseError("Options contain local language/server state; reimport the pack")
        if path.name in {"level.dat", "level.dat_old"}:
            from .world import sanitize_level
            sanitized = sanitize_level(data)
            if (gzip.decompress(sanitized) if sanitized.startswith(b"\x1f\x8b") else sanitized) != \
               (gzip.decompress(data) if data.startswith(b"\x1f\x8b") else data):
                raise ReleaseError("World template has unsanitized player/game-mode state; reimport the pack")
        records.append({"path": relative, "sha512": digest(data), "sha1": digest(data, "sha1"),
                        "size": len(data), "downloads": [], "env": {}, "local": str(path.resolve())})
    for path in paths:
        parts = path.split("/")
        if any("/".join(parts[:i]) in paths for i in range(1, len(parts))):
            raise ReleaseError(f"File/directory collision: {path}")
    return index, sorted(records, key=lambda r: r["path"])


def materialize_record(record, root=ROOT):
    if record.get("local"):
        data = Path(record["local"]).read_bytes()
    else:
        cached = Path(root) / ".cache/downloads" / record["sha512"]
        data = cached.read_bytes() if cached.exists() else None
        if data is None:
            for url in record["downloads"]:
                if urllib.parse.urlsplit(url).hostname not in DOWNLOAD_HOSTS:
                    raise ReleaseError("Untrusted download host")
                try:
                    data = request_bytes(url)
                    break
                except ReleaseError:
                    continue
            if data is None:
                raise ReleaseError(f"Cannot download {record['path']}")
            if len(data) != record["size"] or digest(data) != record["sha512"] or digest(data, "sha1") != record["sha1"]:
                raise ReleaseError(f"Downloaded file does not match locked bytes: {record['path']}")
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(data)
    if len(data) != record["size"] or digest(data) != record["sha512"] or digest(data, "sha1") != record["sha1"]:
        raise ReleaseError(f"File hash/size mismatch: {record['path']}")
    return data


def settings(root=ROOT):
    project = read_json(Path(root) / "config/project.json")
    release = read_json(Path(root) / "release/release.json")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,99}", release.get("version", "")):
        raise ReleaseError("Set a version using letters, digits, ., +, _ or - (maximum 100 characters)")
    if release.get("channel") not in {"release", "beta", "alpha"}:
        raise ReleaseError("Release channel must be release, beta or alpha")
    if project.get("branch") != "main":
        raise ReleaseError("FrLO uses the single main branch")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", project.get("github_repository", "")):
        raise ReleaseError("Invalid GitHub repository (owner/name)")
    return project, release


def write_zip(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(entries.items()):
            safe_path(name)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)


def build(root=ROOT, formats=("modrinth",), log=print):
    root = Path(root)
    project, release = settings(root)
    index, records = collect_records(root / "pack")
    index.update(name=project["name"], versionId=release["version"])
    output = root / "dist" / release["version"]
    output.mkdir(parents=True, exist_ok=True)
    artifacts = []
    if "modrinth" in formats:
        entries = {"modrinth.index.json": (json.dumps(index, ensure_ascii=False, indent=2) + "\n").encode("utf-8")}
        entries.update({"overrides/" + r["path"]: materialize_record(r, root) for r in records if r.get("local")})
        artifact = output / f"FrLO-{release['version']}.mrpack"
        write_zip(artifact, entries)
        artifacts.append(artifact)
    if "curseforge" in formats:
        from .curseforge import build as build_cf
        artifacts.append(build_cf(root, records, index, project, release,
                                  output / f"FrLO-{release['version']}-CurseForge.zip", log))
    if not artifacts:
        raise ReleaseError("No output formats selected")
    notes = []
    for filename in ("release/manual.md", "release/changes.md"):
        if (root / filename).exists():
            notes.append((root / filename).read_text(encoding="utf-8"))
    (output / "CHANGELOG.md").write_text("\n\n".join(notes), encoding="utf-8")
    (output / "SHA256SUMS.txt").write_text("".join(f"{digest(p.read_bytes(), 'sha256')}  {p.name}\n" for p in artifacts), encoding="utf-8")
    write_json(output / "build.json", {"version": release["version"], "channel": release["channel"],
               "minecraft": index["dependencies"]["minecraft"], "source_sha512": digest(json.dumps(index, sort_keys=True).encode()),
               "artifacts": {p.name: digest(p.read_bytes()) for p in artifacts}})
    for path in artifacts:
        log(f"Built: {path}")
    return output, artifacts


def run(args, root=ROOT, *, input=None, check=True):
    if args and args[0] == "git":
        args = ["git", "-c", "safe.directory=" + str(Path(root).resolve()), *args[1:]]
    stream_input = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    result = subprocess.run([str(a) for a in args], cwd=root, **stream_input, encoding="utf-8",
                            errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        # gh/git errors do not include token values; redact any supplied secrets defensively.
        error = result.stderr.strip()
        for name in ("MODRINTH_TOKEN", "CURSEFORGE_TOKEN", "CF_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
            if os.environ.get(name):
                error = error.replace(os.environ[name], "[REDACTED]")
        raise ReleaseError(f"{args[0]} failed: {error}")
    return result


def multipart(fields, files):
    boundary = "frlo-" + uuid.uuid4().hex
    stream = io.BytesIO()
    for name, value in fields.items():
        stream.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode())
        stream.write(json.dumps(value).encode("utf-8") if not isinstance(value, str) else value.encode("utf-8"))
        stream.write(b"\r\n")
    for name, path in files.items():
        stream.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode())
        stream.write(path.read_bytes())
        stream.write(b"\r\n")
    stream.write(f"--{boundary}--\r\n".encode())
    return stream.getvalue(), {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def publish(root=ROOT, targets=("github", "modrinth"), log=print):
    root = Path(root)
    project, release = settings(root)
    index, _ = collect_records(root / "pack")
    repository = project["github_repository"]
    manual_notes = (root / "release/manual.md").read_text(encoding="utf-8").strip()
    if not manual_notes or "在这里填写本次更新" in manual_notes:
        raise ReleaseError("Replace the placeholder in the release notes before publishing")
    # Complete validation before any external mutation.
    for target, name in (("modrinth", "MODRINTH_TOKEN"), ("curseforge", "CURSEFORGE_TOKEN")):
        if target in targets and not os.environ.get(name):
            raise ReleaseError(f"Missing {name}; enter it in the GUI or add an Actions Secret")
    if "modrinth" in targets and not project.get("modrinth_project_id"):
        raise ReleaseError("Set Modrinth project ID")
    if "curseforge" in targets and not str(project.get("curseforge_project_id", "")).isdigit():
        raise ReleaseError("Create a CurseForge project and enter its numeric project ID first")
    if "curseforge" in targets and "github" not in targets and os.environ.get("GITHUB_ACTIONS") == "true":
        raise ReleaseError("Cloud CurseForge publishing also requires GitHub for a durable upload receipt")
    if "github" in targets:
        run(["gh", "auth", "status"], root)
        run(["gh", "api", f"repos/{repository}"], root)
        if run(["git", "branch", "--show-current"], root).stdout.strip() != "main":
            raise ReleaseError("GitHub releases must use main")
        if run(["git", "status", "--porcelain", "--untracked-files=normal"], root).stdout.strip():
            raise ReleaseError("Commit source/config changes before local GitHub publishing, or use cloud publish")
        commit = run(["git", "rev-parse", "HEAD"], root).stdout.strip()
        remote_commit = json.loads(run(["gh", "api", f"repos/{repository}/commits/main"], root).stdout)["sha"]
        if commit != remote_commit:
            raise ReleaseError("Push the current main commit before publishing")
    formats = tuple(f for f in ("modrinth", "curseforge") if f == "modrinth" or "curseforge" in targets)
    output, artifacts = build(root, formats, log)
    artifact_map = {"modrinth": output / f"FrLO-{release['version']}.mrpack",
                    "curseforge": output / f"FrLO-{release['version']}-CurseForge.zip"}
    receipt_path = output / "publish-result.json"
    receipt = read_json(receipt_path) if receipt_path.exists() else {"version": release["version"], "platforms": {}}
    if receipt.get("version") != release["version"] or not isinstance(receipt.get("platforms"), dict):
        raise ReleaseError("Invalid release receipt; inspect the saved state")
    notes = (output / "CHANGELOG.md").read_text(encoding="utf-8")
    tag = "v" + release["version"]
    # GitHub draft is a durable recovery journal for partially completed multi-platform uploads.
    if "github" in targets:
        tag_result = run(["gh", "api", f"repos/{repository}/git/ref/tags/{urllib.parse.quote(tag, safe='')}"], root, check=False)
        if tag_result.returncode:
            try:
                missing = str(json.loads(tag_result.stdout).get("status")) == "404"
            except ValueError:
                missing = False
            if not missing:
                raise ReleaseError("Cannot verify the existing GitHub tag; check connectivity and permissions")
        else:
            tagged_commit = json.loads(run(["gh", "api", f"repos/{repository}/commits/{urllib.parse.quote(tag, safe='')}"], root).stdout)["sha"]
            if tagged_commit != commit:
                raise ReleaseError("Existing Git tag points to different source; use a new version")
        existing = run(["gh", "release", "view", tag, "--repo", repository, "--json", "databaseId,isDraft,targetCommitish,assets"], root, check=False)
        if existing.returncode:
            args = ["gh", "release", "create", tag, "--repo", repository, "--target", commit, "--draft",
                    "--title", f"FrLO {release['version']}", "--notes-file", str(output / "CHANGELOG.md")]
            if release["channel"] != "release":
                args.append("--prerelease")
            run(args, root)
        else:
            existing_info = json.loads(existing.stdout)
            if existing_info["isDraft"]:
                if existing_info["targetCommitish"] != commit:
                    raise ReleaseError("Existing release draft uses different source; use a new version")
            else:
                tag_commit = json.loads(run(["gh", "api", f"repos/{repository}/commits/{urllib.parse.quote(tag, safe='')}"], root).stdout)["sha"]
                if tag_commit != commit:
                    raise ReleaseError("Existing release tag points to different source; use a new version")
            asset_names = {item["name"] for item in existing_info["assets"]}
            if "publish-result.json" in asset_names:
                journal_dir = root / ".cache" / "remote-receipt"
                journal_dir.mkdir(parents=True, exist_ok=True)
                run(["gh", "release", "download", tag, "--repo", repository, "--pattern", "publish-result.json",
                     "--dir", journal_dir, "--clobber"], root)
                remote_receipt = read_json(journal_dir / "publish-result.json")
                if remote_receipt.get("version") != release["version"] or not isinstance(remote_receipt.get("platforms"), dict):
                    raise ReleaseError("Invalid remote release receipt")
                receipt = remote_receipt
            # Immutable CF markers survive a failed --clobber journal update.
            marker_name = "curseforge-uploaded.json" if "curseforge-uploaded.json" in asset_names else "curseforge-pending.json"
            if marker_name in asset_names:
                journal_dir = root / ".cache" / "remote-receipt"
                journal_dir.mkdir(parents=True, exist_ok=True)
                run(["gh", "release", "download", tag, "--repo", repository, "--pattern", marker_name,
                     "--dir", journal_dir, "--clobber"], root)
                marker = read_json(journal_dir / marker_name)
                if marker.get("version") != release["version"]:
                    raise ReleaseError("CurseForge receipt version mismatch")
                if marker_name == "curseforge-uploaded.json" or "curseforge" not in receipt["platforms"]:
                    receipt["platforms"]["curseforge"] = marker["curseforge"]
            for path in artifacts:
                if path.name in asset_names:
                    asset_dir = root / ".cache" / "remote-assets"
                    asset_dir.mkdir(parents=True, exist_ok=True)
                    run(["gh", "release", "download", tag, "--repo", repository, "--pattern", path.name,
                         "--dir", asset_dir, "--clobber"], root)
                    if digest((asset_dir / path.name).read_bytes()) != digest(path.read_bytes()):
                        raise ReleaseError(f"Existing GitHub asset differs: {path.name}; use a new version")
        # Upload only missing assets. Never replace an existing version's pack.
        names = {a["name"] for a in json.loads(run(["gh", "release", "view", tag, "--repo", repository,
                                                   "--json", "assets"], root).stdout)["assets"]}
        for path in artifacts + [output / "SHA256SUMS.txt", output / "CHANGELOG.md", output / "build.json"]:
            if path.name not in names:
                run(["gh", "release", "upload", tag, path, "--repo", repository], root)
    def record(platform, value, hash_value):
        receipt["platforms"][platform] = {**value, "sha512": hash_value}
        write_json(receipt_path, receipt)
        if "github" in targets:
            run(["gh", "release", "upload", tag, receipt_path, "--repo", repository, "--clobber"], root)
    def cf_marker(name, value, hash_value):
        marker_path = output / name
        write_json(marker_path, {"version": release["version"], "curseforge": {**value, "sha512": hash_value}})
        if "github" in targets:
            # This marker is intentionally never replaced or deleted.
            run(["gh", "release", "upload", tag, marker_path, "--repo", repository], root)
    for platform in ("modrinth", "curseforge"):
        if platform not in targets:
            continue
        path = artifact_map[platform]
        hash_value = digest(path.read_bytes())
        if platform in receipt["platforms"]:
            previous = receipt["platforms"][platform]
            if previous["sha512"] != hash_value or str(previous.get("project_id")) != str(project[f"{platform}_project_id"]):
                raise ReleaseError(f"Published {platform} artifact differs; use a new version")
            if previous.get("status") == "upload-pending-verification":
                raise ReleaseError("A previous CurseForge upload has an uncertain result. Check the project files, "
                                   "then reconcile publish-result.json with the actual file ID before retrying")
            log(f"{platform}: already uploaded; skipping")
            continue
        if platform == "modrinth":
            headers = {"Authorization": os.environ["MODRINTH_TOKEN"]}
            project_id = urllib.parse.quote(project["modrinth_project_id"], safe="")
            versions = http_json(f"https://api.modrinth.com/v2/project/{project_id}/version", headers=headers)
            matches = [v for v in versions if v["version_number"] == release["version"]]
            if matches:
                if len(matches) != 1 or not any(f["hashes"].get("sha512") == hash_value for f in matches[0]["files"]):
                    raise ReleaseError("Existing Modrinth version differs; use a new version")
                result = matches[0]
            else:
                loader = next(LOADERS[key] for key in LOADERS if key in index["dependencies"])
                metadata = {"name": f"FrLO {release['version']}", "version_number": release["version"],
                            "changelog": notes, "dependencies": [], "game_versions": [index["dependencies"]["minecraft"]],
                            "version_type": release["channel"], "loaders": [loader], "featured": False,
                            "project_id": project["modrinth_project_id"], "file_parts": ["pack"], "primary_file": "pack",
                            "environment": project.get("environment", "client_and_server")}
                body, content_headers = multipart({"data": metadata}, {"pack": path})
                result = http_json("https://api.modrinth.com/v2/version", "POST", body, {**headers, **content_headers})
            record(platform, {"id": result["id"], "project_id": project["modrinth_project_id"]}, hash_value)
        else:
            metadata = {"changelog": notes, "changelogType": "markdown", "displayName": f"FrLO {release['version']}",
                        "gameVersionNames": [index["dependencies"]["minecraft"],
                            next(LOADERS[key].title().replace("Neoforge", "NeoForge") for key in LOADERS if key in index["dependencies"])],
                        "releaseType": release["channel"]}
            body, content_headers = multipart({"metadata": metadata}, {"file": path})
            pending = {"project_id": project["curseforge_project_id"], "status": "upload-pending-verification"}
            cf_marker("curseforge-pending.json", pending, hash_value)
            record(platform, pending, hash_value)
            result = http_json(f"https://minecraft.curseforge.com/api/projects/{project['curseforge_project_id']}/upload-file",
                               "POST", body, {"X-Api-Token": os.environ["CURSEFORGE_TOKEN"], **content_headers})
            uploaded = {"id": result["id"], "project_id": project["curseforge_project_id"], "status": "submitted-for-review"}
            cf_marker("curseforge-uploaded.json", uploaded, hash_value)
            record(platform, uploaded, hash_value)
        log(f"{platform}: uploaded (receipt saved)")
    if "github" in targets:
        run(["gh", "release", "edit", tag, "--repo", repository, "--draft=false"], root)
        record("github", {"url": f"https://github.com/{repository}/releases/tag/{tag}"}, digest((output / "build.json").read_bytes()))
    log(f"Release completed. Receipt: {receipt_path}")
    return receipt


def dispatch(root=ROOT, targets=("github", "modrinth"), log=print):
    root = Path(root)
    project, release = settings(root)
    collect_records(root / "pack")
    if "curseforge" in targets and not str(project.get("curseforge_project_id", "")).isdigit():
        raise ReleaseError("Create a CurseForge project and enter its numeric ID first")
    if "curseforge" in targets and "github" not in targets:
        raise ReleaseError("Select GitHub with CurseForge for cloud publishing so upload receipts survive retries")
    if run(["git", "rev-parse", "--show-toplevel"], root).stdout.strip().replace("\\", "/").casefold() != root.resolve().as_posix().casefold():
        raise ReleaseError("Publishing folder must be its own Git repository")
    if run(["git", "branch", "--show-current"], root).stdout.strip() != "main":
        raise ReleaseError("Switch to main before dispatching")
    if run(["git", "diff", "--cached", "--name-only"], root).stdout.strip():
        raise ReleaseError("The Git index already contains staged files. Commit or unstage them before cloud publishing")
    origin = run(["git", "remote", "get-url", "origin"], root).stdout.strip().removesuffix(".git")
    if origin not in {"https://github.com/" + project["github_repository"], "git@github.com:" + project["github_repository"]}:
        raise ReleaseError("Git origin does not match configured GitHub repository")
    secret_names = {s["name"] for s in json.loads(run(["gh", "secret", "list", "--repo", project["github_repository"], "--json", "name"], root).stdout)}
    needed = {"modrinth": "MODRINTH_TOKEN", "curseforge": "CURSEFORGE_TOKEN"}
    for target in targets:
        if target in needed and needed[target] not in secret_names:
            raise ReleaseError(f"Add {needed[target]} to Actions Secrets first")
    # Explicit owned paths; never commit generated files or arbitrary parent workspace contents.
    run(["git", "add", "--", ".github", ".gitignore", "frlo_release", "config", "release/release.json",
         "release/manual.md", "release/changes.md", "pack", "pyproject.toml", "README.md", "发布FrLO.cmd", "build-gui.ps1", "gui-entry.py", ".gitattributes"], root)
    if run(["git", "diff", "--cached", "--quiet"], root, check=False).returncode:
        run(["git", "commit", "-m", f"Prepare FrLO {release['version']}"], root)
    run(["git", "push", "origin", "main"], root)
    run(["gh", "workflow", "run", "publish.yml", "--repo", project["github_repository"], "--ref", "main",
         "-f", "targets=" + ",".join(targets)], root)
    log(f"Cloud publish started: https://github.com/{project['github_repository']}/actions/workflows/publish.yml")
