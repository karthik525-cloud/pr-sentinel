"""Offline wheel content guard, including the packaged benchmark fixture."""

from pathlib import Path
from zipfile import ZipFile


def main() -> None:
    wheels = list(Path("dist").glob("pr_sentinel-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Expected one project wheel")
    with ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        assert "benchmarks/v1.json" in names
        assert "app/main.py" in names
        assert any(name.endswith(".dist-info/licenses/LICENSE") for name in names)
        for name in names:
            assert not any(
                part in {".env", ".git", ".venv", "evaluation-runs", "__pycache__"}
                or part.startswith(".env.")
                for part in Path(name).parts
            )
    print("Wheel includes application and benchmark data; no excluded paths.")


if __name__ == "__main__":
    main()
