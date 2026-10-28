"""Lossless, fail-closed mrpack to CurseForge manifest conversion.

The fingerprint is only a search key: SHA-1 and file length must also agree.
No redistribution permission is inferred from a download URL or fingerprint.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import urllib.request
from urllib.parse import urlparse
import zipfile

API = "https://api.curseforge.com/v1"
# Minecraft CurseForge project classes determine the launcher's install directory.
CLASSES = {6: "mods", 12: "resourcepacks", 6552: "shaderpacks"}
CANDIDATES = frozenset(CLASSES.values())
LOADER_IDS = {"fabric-loader": "fabric-", "quilt-loader": "quilt-",
              "forge": "forge-", "neoforge": "neoforge-"}


def fingerprint(data: bytes) -> int:
    """CurseForge's 32-bit MurmurHash2, seed 1, excluding TAB/LF/CR/space."""
    data = data.translate(None, b"\x09\x0a\x0d\x20")
    mask, multiplier = 0xffffffff, 0x5bd1e995
    value = 1 ^ len(data)
    end = len(data) - len(data) % 4
    for offset in range(0, end, 4):
        word = int.from_bytes(data[offset:offset + 4], "little")
        word = (word * multiplier) & mask
        word ^= word >> 24
        word = (word * multiplier) & mask
        value = ((value * multiplier) & mask) ^ word
    tail = data[end:]
    if tail:
        value ^= int.from_bytes(tail, "little")
        value = (value * multiplier) & mask
    value ^= value >> 13
    value = (value * multiplier) & mask
    return (value ^ (value >> 15)) & mask


def _json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     delete=False, suffix=".tmp") as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _lock(root: Path) -> dict:
    path = root / "config/curseforge-lock.json"
    value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "schema": 1, "files": {}, "overrides": {}}
    if (value.get("schema") != 1 or not isinstance(value.get("files"), dict)
            or not isinstance(value.get("overrides"), dict)):
        raise ValueError("Invalid config/curseforge-lock.json schema")
    return value


def _path(record: dict) -> PurePosixPath:
    raw = record["path"]
    if not isinstance(raw, str):
        raise ValueError("Game path must be a string")
    path = PurePosixPath(raw)
    if (not raw or "\\" in raw or "\x00" in raw or path.is_absolute()
            or str(path) != raw or any(p in (".", "..") for p in path.parts)
            or any(":" in p or p.endswith((".", " ")) for p in path.parts)):
        raise ValueError(f"Unsafe game path: {raw!r}")
    return path


def _check_records(records: list) -> None:
    seen = set()
    paths = []
    for record in records:
        path = str(_path(record)).casefold()
        if path in seen:
            raise ValueError(f"Duplicate game path: {record['path']}")
        if not re.fullmatch(r"[0-9a-fA-F]{128}", record.get("sha512", "")):
            raise ValueError(f"Missing SHA-512: {record['path']}")
        seen.add(path)
        paths.append(path)
    for path in paths:
        parts = path.split("/")
        if any("/".join(parts[:i]) in seen for i in range(1, len(parts))):
            raise ValueError(f"File/directory collision: {path}")


def _client(record: dict) -> str:
    value = (record.get("env") or {}).get("client", "required")
    if value not in ("required", "unsupported", "optional"):
        raise ValueError(f"Unknown client environment: {record['path']}")
    return value


def _candidate(record: dict) -> bool:
    return _path(record).parts[0] in CANDIDATES


def _approval(value: object) -> bool:
    if not isinstance(value, dict) or value.get("approved") is not True:
        return False
    source = value.get("source")
    if not isinstance(source, str) or not isinstance(value.get("license"), str):
        return False
    url = urlparse(source)
    return bool(value["license"].strip() and url.scheme == "https" and url.hostname
                and not url.username and not url.password and not url.fragment)


def _bytes(record: dict, root: Path) -> bytes:
    from .core import materialize_record
    data = materialize_record(record, root)
    if (len(data) != record["size"]
            or hashlib.sha512(data).hexdigest() != record["sha512"].lower()
            or (record.get("sha1") and hashlib.sha1(data).hexdigest() != record["sha1"].lower())):
        raise ValueError(f"Content hash/size mismatch: {record['path']}")
    return data


def _request(route: str, key: str, data=None) -> dict:
    from .core import http_json
    headers = {"x-api-key": key, "Accept": "application/json"}
    try:
        return http_json(API + route, method="POST" if data is not None else "GET",
                         data=data, headers=headers)
    except Exception as exc:
        # Fingerprint POST is a read-only lookup, safe to retry after transport
        # failure. Core intentionally does not retry arbitrary upload POSTs.
        if data is None or "network failure" not in str(exc):
            raise
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            raise ValueError("Unexpected CurseForge API redirect")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": "http://127.0.0.1:10808"}), NoRedirect())
    request = urllib.request.Request(API + route, data=json.dumps(data).encode("utf-8"),
                                     method="POST", headers={**headers, "Content-Type": "application/json"})
    with opener.open(request, timeout=90) as response:
        return json.load(response)


def _compatible(versions: object, record: dict, index: dict) -> bool:
    if not isinstance(versions, list) or not all(isinstance(item, str) for item in versions):
        return False
    dependencies = index.get("dependencies", {})
    if dependencies.get("minecraft") not in versions:
        return False
    if _path(record).parts[0] != "mods":
        return True
    labels = {"fabric-loader": "Fabric", "quilt-loader": "Quilt",
              "forge": "Forge", "neoforge": "NeoForge"}
    loaders = [labels[name] for name in dependencies if name in labels]
    return len(loaders) == 1 and loaders[0] in versions


def _pin_valid(pin: object, record: dict, sha1: str, index: dict) -> bool:
    if not isinstance(pin, dict):
        return False
    path = _path(record)
    return (type(pin.get("project_id")) is int and pin["project_id"] > 0
            and type(pin.get("file_id")) is int and pin["file_id"] > 0
            and pin.get("file_name") == path.name
            and pin.get("sha1", "").lower() == sha1
            and len(path.parts) == 2
            and CLASSES.get(pin.get("class_id")) == path.parts[0]
            and _compatible(pin.get("game_versions"), record, index))


def resolve(root: Path, records: list, index: dict, log=print, *, allow_network=True) -> dict:
    """Save exact verified pins; report every file that cannot be represented.

    Successful pins include class_id and game_versions so offline builds retain
    install-directory and loader checks. Legacy pins are refreshed via the API.
    """
    root = Path(root)
    _check_records(records)
    lock = _lock(root)
    key = os.environ.get("CF_API_KEY", "").strip() if allow_network else ""
    unresolved, resolved, overrides, excluded, pending = [], [], [], [], []

    def problem(record, reason):
        unresolved.append({"path": record["path"], "sha512": record["sha512"],
                           "sha1": record.get("sha1"), "reason": reason})

    for record in records:
        environment = _client(record)
        if environment == "unsupported":
            excluded.append(record["path"])
            continue
        if environment == "optional":
            problem(record, "optional client files cannot be represented losslessly")
            continue
        if not _candidate(record):
            overrides.append(record["path"])
            continue
        sha1 = record.get("sha1", "").lower()
        digest = record["sha512"].lower()
        pin = lock["files"].get(digest)
        if _pin_valid(pin, record, sha1, index):
            resolved.append({"path": record["path"], "sha512": digest, **pin})
        elif _approval(lock["overrides"].get(digest)):
            overrides.append(record["path"])
        elif key:
            log(f"CurseForge: reading verified bytes for {record['path']}")
            try:
                data = _bytes(record, root)
            except Exception as exc:
                problem(record, "cannot materialize verified bytes (" + type(exc).__name__ + ")")
                continue
            pending.append((record, hashlib.sha1(data).hexdigest(), fingerprint(data)))
        else:
            problem(record, "no verified CurseForge pin or approved redistribution; set CF_API_KEY to resolve")

    project_classes = {}
    for start in range(0, len(pending), 100):
        batch = pending[start:start + 100]
        log(f"CurseForge: fingerprint batch {start // 100 + 1}, {len(batch)} files")
        try:
            response = _request("/fingerprints/432", key,
                                {"fingerprints": sorted({item[2] for item in batch})})
            matches = response["data"]["exactMatches"]
            if not isinstance(matches, list):
                raise ValueError("Malformed fingerprint response")
        except Exception as exc:
            for record, _, _ in batch:
                problem(record, "fingerprint lookup failed (" + type(exc).__name__ + ")")
            continue
        for record, sha1, stamp in batch:
            valid, reasons = {}, []
            for match in matches:
                file = match.get("file", {})
                if file.get("fileFingerprint") != stamp:
                    continue
                hashes = {h.get("value", "").lower() for h in file.get("hashes", [])
                          if h.get("algo") == 1}
                if sha1 not in hashes or file.get("fileLength") != record["size"]:
                    reasons.append("fingerprint matched but SHA-1/length differs")
                    continue
                if not _compatible(file.get("gameVersions"), record, index):
                    reasons.append("CurseForge Minecraft version or loader is incompatible")
                    continue
                path = _path(record)
                if len(path.parts) != 2 or file.get("fileName") != path.name:
                    reasons.append("CurseForge install filename/path differs")
                    continue
                project_id, file_id = file.get("modId"), file.get("id")
                if (type(project_id) is not int or project_id <= 0
                        or type(file_id) is not int or file_id <= 0
                        or file.get("gameId") != 432 or file.get("isAvailable") is not True):
                    reasons.append("invalid or unavailable CurseForge file metadata")
                    continue
                try:
                    if project_id not in project_classes:
                        project = _request(f"/mods/{project_id}", key)["data"]
                        if project.get("id") != project_id or project.get("gameId") != 432:
                            raise ValueError("Unexpected project metadata")
                        project_classes[project_id] = project.get("classId")
                    class_id = project_classes[project_id]
                    if CLASSES.get(class_id) != path.parts[0]:
                        reasons.append("CurseForge project installs in a different directory")
                        continue
                except Exception as exc:
                    reasons.append("project metadata lookup failed (" + type(exc).__name__ + ")")
                    continue
                valid[(project_id, file_id)] = {
                    "project_id": project_id, "file_id": file_id,
                    "file_name": file["fileName"], "sha1": sha1, "class_id": class_id,
                    "game_versions": sorted(set(file["gameVersions"]))}
            if len(valid) != 1:
                problem(record, "ambiguous exact matches" if valid else
                        "; ".join(sorted(set(reasons))) or "no exact SHA-1-verified CurseForge match")
                continue
            pin = next(iter(valid.values()))
            digest = record["sha512"].lower()
            lock["files"][digest] = pin
            resolved.append({"path": record["path"], "sha512": digest, **pin})

    # CurseForge rejects repeated projects; never silently choose one version.
    projects = {}
    for item in resolved:
        projects.setdefault(item["project_id"], []).append(item)
    for items in projects.values():
        if len(items) > 1:
            for item in items:
                unresolved.append({"path": item["path"], "sha512": item["sha512"],
                                   "sha1": item["sha1"], "reason": "duplicate CurseForge project in manifest"})
    _json_write(root / "config/curseforge-lock.json", lock)
    report = {"schema": 1, "resolved": sorted(resolved, key=lambda x: x["path"]),
              "overrides": sorted(overrides), "excluded_client_unsupported": sorted(excluded),
              "unresolved": sorted(unresolved, key=lambda x: x["path"])}
    _json_write(root / "build/curseforge-unresolved.json", report)
    log(f"CurseForge: {len(resolved)} mapped, {len(overrides)} overrides, {len(unresolved)} unresolved")
    return report


def build(root: Path, records: list, index: dict, project: dict, release: dict,
          output: Path, log=print) -> Path:
    """Write a deterministic client manifest ZIP, or leave output untouched."""
    root, output = Path(root), Path(output)
    dependencies = index.get("dependencies", {})
    minecraft = dependencies.get("minecraft")
    loaders = [(name, version) for name, version in dependencies.items() if name in LOADER_IDS]
    unknown = set(dependencies) - {"minecraft", *LOADER_IDS}
    if not minecraft or len(loaders) != 1 or unknown:
        raise ValueError("CurseForge requires Minecraft and exactly one supported loader; unknown dependencies rejected")
    # Published artifacts must use committed pins, never mappings discovered
    # during an uncommitted CI build. Resolve explicitly, review, then commit.
    report = resolve(root, records, index, log, allow_network=False)
    if report["unresolved"]:
        raise ValueError(f"CurseForge build blocked: {len(report['unresolved'])} unresolved files; see build/curseforge-unresolved.json")
    loader, loader_version = loaders[0]
    manifest = {"minecraft": {"version": minecraft, "modLoaders": [
        {"id": LOADER_IDS[loader] + loader_version, "primary": True}]},
        "manifestType": "minecraftModpack", "manifestVersion": 1,
        "name": project["name"], "version": release["version"], "author": project["author"],
        "files": [{"projectID": item["project_id"], "fileID": item["file_id"], "required": True}
                  for item in sorted(report["resolved"], key=lambda x: (x["project_id"], x["file_id"]))],
        "overrides": "overrides"}
    override_paths = set(report["overrides"])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".zip", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            entries = [("manifest.json", None)] + [
                ("overrides/" + record["path"], record) for record in records
                if record["path"] in override_paths]
            for name, record in sorted(entries, key=lambda item: item[0]):
                data = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8") if record is None else _bytes(record, root)
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    log(f"Built CurseForge archive: {output}")
    return output
