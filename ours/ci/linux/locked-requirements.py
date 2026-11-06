"""Export the registry wheels from the trusted OMS baseline for this check image."""
import re
import sys
import tomllib
from pathlib import Path


def main(path: Path) -> None:
    packages = tomllib.loads(path.read_text())["package"]
    for item in packages:
        if item["name"] == "oh-my-symphony" and item["source"] == {"editable": "."}:
            continue
        if item["source"] != {"registry": "https://pypi.org/simple"}:
            raise ValueError("Unexpected package source")
        name, version = item["name"], item["version"]
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name) or not re.fullmatch(r"[a-zA-Z0-9.+!-]+", version):
            raise ValueError("Invalid locked requirement")
        hashes = sorted({w["hash"] for w in item["wheels"]})
        if not hashes or not all(re.fullmatch(r"sha256:[a-f0-9]{64}", h) for h in hashes):
            raise ValueError("Missing wheel hashes for " + name)
        print(f"{name}=={version}", *("--hash=" + h for h in hashes))
    # Build the project's editable install offline inside each fresh container.
    print("setuptools==80.9.0 --hash=sha256:062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922")
    print("wheel==0.45.1 --hash=sha256:708e7481cc80179af0e556bbf0cc00b8444c7321e2700b8d8580231d13017248")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
