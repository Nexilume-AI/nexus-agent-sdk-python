"""Reject unexpected wheel payloads and untracked or changed sdist sources."""
from pathlib import Path
from email.parser import BytesParser
import hashlib
import tarfile
import zipfile
import tomllib

root = Path(__file__).resolve().parents[1]
project = tomllib.loads((root / 'pyproject.toml').read_text())['project']
name = project['name'].replace('-', '_')
version = project['version']
stem = name + '-' + version
wheel = root / 'dist' / (stem + '-py3-none-any.whl')
sdist = root / 'dist' / (stem + '.tar.gz')
expected = {p.relative_to(root / 'src').as_posix(): p.read_bytes()
            for p in (root / 'src/nexus_agent').rglob('*.py')}
prefix = stem + '.dist-info/'
metadata_names = {'METADATA', 'WHEEL', 'entry_points.txt', 'top_level.txt', 'RECORD', 'licenses/LICENSE'}
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    assert len(names) == len(set(names)), 'duplicate wheel entries'
    assert set(names) == set(expected) | {prefix + n for n in metadata_names}, 'unexpected wheel content'
    for path, body in expected.items():
        assert archive.read(path) == body, path
    assert archive.read(prefix+'licenses/LICENSE') == (root/'LICENSE').read_bytes()
    metadata = BytesParser().parsebytes(archive.read(prefix+'METADATA'))
    assert metadata['Name'] == project['name'] and metadata['Version'] == version
    assert metadata['License-Expression'] == 'Apache-2.0'
    assert metadata['Description-Content-Type'] == 'text/markdown'
    assert metadata['Requires-Python'] == project['requires-python']
with tarfile.open(sdist) as archive:
    seen = set()
    for member in archive.getmembers():
        assert member.name not in seen, 'duplicate sdist entry'
        seen.add(member.name)
        path = Path(member.name)
        assert not path.is_absolute() and '..' not in path.parts
        assert path.parts[0] == stem
        assert member.isdir() or member.isfile(), 'sdist link/device'
        if not member.isfile(): continue
        rel = Path(*path.parts[1:])
        assert not any(p.startswith('.') for p in rel.parts), rel
        if rel.as_posix() in {'PKG-INFO', 'setup.cfg'} or '.egg-info' in rel.as_posix():
            continue
        source = root / rel
        assert source.is_file() and archive.extractfile(member).read() == source.read_bytes(), str(rel)
for artifact in (wheel, sdist):
    print(hashlib.sha256(artifact.read_bytes()).hexdigest() + '  ' + artifact.name)
