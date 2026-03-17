from __future__ import annotations

import argparse
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_SOURCE = REPO_ROOT / ".env"
DEFAULT_TEMPLATE = REPO_ROOT / "recommendation_app" / "go-live" / ".env.example"
DEFAULT_TARGET = REPO_ROOT / "recommendation_app" / "go-live" / ".env"


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = value
    return values


def _build_target(template_path: Path, source_values: dict[str, str]) -> str:
    rendered: list[str] = []
    for raw_line in template_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            rendered.append(raw_line)
            continue
        key, default_value = raw_line.split("=", 1)
        resolved = source_values.get(key.strip(), default_value)
        rendered.append(f"{key}={resolved}")
    return "\n".join(rendered) + "\n"


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap recommendation_app/go-live/.env from the root .env and the go-live template."
    )
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="Source env file. Default: repo root .env")
    parser.add_argument(
        "--template",
        default=str(DEFAULT_TEMPLATE),
        help="Template env file. Default: recommendation_app/go-live/.env.example",
    )
    parser.add_argument(
        "--target",
        default=str(DEFAULT_TARGET),
        help="Target env file. Default: recommendation_app/go-live/.env",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write the target file. Exit non-zero if the rendered content differs from the current target.",
    )
    args = parser.parse_args()

    source_path = Path(args.source).resolve()
    template_path = Path(args.template).resolve()
    target_path = Path(args.target).resolve()

    if not template_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_path}")

    source_values = _parse_env_file(source_path)
    rendered = _build_target(template_path, source_values)
    current = target_path.read_text(encoding="utf-8") if target_path.exists() else ""

    if args.check:
        if current != rendered:
            print(f"out_of_sync: {_relative(target_path)}")
            return 1
        print(f"in_sync: {_relative(target_path)}")
        return 0

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(rendered, encoding="utf-8")
    print(f"written: {_relative(target_path)}")
    print(f"template: {_relative(template_path)}")
    print(f"source: {_relative(source_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
