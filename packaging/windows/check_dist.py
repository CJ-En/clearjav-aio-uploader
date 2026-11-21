"""Refuse a build that names the computer it was made on: searches every file of the folder for the given
words (user name, folders, accounts), as UTF-8 and UTF-16 and ignoring case. Exit code 1 when one is found."""

import sys
from pathlib import Path


def main() -> int:
    folder = Path(sys.argv[1])
    words = [w for w in sys.argv[2:] if len(w) >= 4]
    needles = []
    for word in words:
        for variant in {word, word.replace("\\", "/"), word.lower(), word.upper()}:
            needles += [variant.encode("utf-8"), variant.encode("utf-16-le")]
    found = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        data = path.read_bytes()
        lowered = data.lower()
        for needle in needles:
            if needle in data or needle.lower() in lowered:
                found.append(f"{path.relative_to(folder)}: contains {needle.decode('utf-8', 'replace').replace(chr(0), '')!r}")
                break
    for line in found:
        print(line)
    print(f"checked {sum(1 for p in folder.rglob('*') if p.is_file())} files for {len(words)} words: "
          + ("NOT CLEAN" if found else "clean"))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
