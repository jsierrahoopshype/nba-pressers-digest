"""
Copy the clip-tool installers to docs/presser-clips/ so GitHub Pages serves
them as direct downloads (no GitHub account needed):

    install-presser-clips.bat          Windows (CRLF bytes, as committed)
    install-presser-clips-mac.zip      macOS: the .command inside keeps its
                                       "executable" flag, which a plain
                                       browser download would lose

    python pressers_v2/tools/build_installers.py

Deterministic (fixed zip timestamps), so rebuilding without changes leaves
the files identical; a test checks they match the sources.
"""

import shutil
import sys
import zipfile
from pathlib import Path

PV2 = Path(__file__).resolve().parent.parent
DOCS = PV2.parent / "docs" / "presser-clips"
WIN = "install-presser-clips.bat"
MAC = "install-presser-clips-mac.command"
MAC_ZIP = "install-presser-clips-mac.zip"
ZIP_DATE = (2026, 1, 1, 0, 0, 0)


def mac_zip_bytes_to(path: Path) -> None:
    info = zipfile.ZipInfo(MAC, date_time=ZIP_DATE)
    info.create_system = 3                       # unix, so Archive Utility applies the mode
    info.external_attr = (0o100755 & 0xFFFF) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(info, (PV2 / MAC).read_bytes())


def build(dest: Path = DOCS) -> list:
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PV2 / WIN, dest / WIN)
    mac_zip_bytes_to(dest / MAC_ZIP)
    return [dest / WIN, dest / MAC_ZIP]


if __name__ == "__main__":
    for p in build(Path(sys.argv[1]) if len(sys.argv) > 1 else DOCS):
        print(f"wrote {p}")
