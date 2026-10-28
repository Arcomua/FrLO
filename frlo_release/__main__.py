import argparse
import sys
import zipfile
from pathlib import Path

from . import core


def selections(text, allowed):
    values = tuple(dict.fromkeys(text.split(",")))
    if not values or set(values) - allowed:
        raise core.ReleaseError("Choose from: " + ", ".join(sorted(allowed)))
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(description="FrLO 单分支预处理、打包与发布")
    parser.add_argument("--root", type=Path, default=core.ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("gui", help="打开发布界面")
    importing = commands.add_parser("import", help="导入启动器导出的 .mrpack")
    importing.add_argument("source", type=Path)
    building = commands.add_parser("build", help="本地打包；不上传")
    building.add_argument("--formats", default="modrinth")
    commands.add_parser("resolve-curseforge", help="匹配 CurseForge 文件 ID，需 CF_API_KEY")
    for name in ("publish", "dispatch"):
        publishing = commands.add_parser(name, help="本地发布" if name == "publish" else "提交源码并启动云端发布")
        publishing.add_argument("--targets", default="github,modrinth")
    args = parser.parse_args(argv)
    try:
        if args.command == "gui":
            from .gui import main as launch
            launch()
        elif args.command == "import":
            core.import_pack(args.source, args.root)
        elif args.command == "build":
            core.build(args.root, selections(args.formats, {"modrinth", "curseforge"}))
        elif args.command == "resolve-curseforge":
            from .curseforge import resolve
            index, records = core.collect_records(args.root / "pack")
            report = resolve(args.root, records, index)
            if report["unresolved"]:
                raise core.ReleaseError(f"{len(report['unresolved'])} unresolved CurseForge files; see build/curseforge-unresolved.json")
        else:
            getattr(core, args.command)(args.root, selections(args.targets, {"github", "modrinth", "curseforge"}))
        return 0
    except (core.ReleaseError, ValueError, KeyError, OSError, zipfile.BadZipFile) as exc:
        message = str(exc)
        import os
        for key in ("MODRINTH_TOKEN", "CURSEFORGE_TOKEN", "CF_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
            if os.environ.get(key):
                message = message.replace(os.environ[key], "[REDACTED]")
        print("ERROR: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
