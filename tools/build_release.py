"""Create a source distribution from an explicit allowlist, never runtime data."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ['README.md', 'SECURITY.md', 'CONTRIBUTING.md', 'CHANGELOG.md',
             'Start HAM Cloud UDP Bridge.command']


def source_files():
    files = [ROOT/name for name in DOCUMENTS]
    if (ROOT/'LICENSE').exists():
        files.append(ROOT/'LICENSE')
    for folder, suffixes in [('ham_cloud_udp_bridge', {'.py', '.html', '.css', '.js'}),
                             ('tests', {'.py'}), ('docs', {'.md'}), ('tools', {'.py'})]:
        files += [p for p in (ROOT/folder).rglob('*') if p.is_file()
                  and not p.is_symlink() and p.suffix in suffixes and '__pycache__' not in p.parts]
    return sorted(files)


def main():
    destination = ROOT/'dist/HAM-Cloud-UDP-Bridge-macOS.zip'
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for file in source_files():
            archive.write(file, Path('HAM Cloud UDP Bridge')/file.relative_to(ROOT))
    print(f'Built {destination.name} ({len(source_files())} source/documentation files)')


if __name__ == '__main__':
    main()
