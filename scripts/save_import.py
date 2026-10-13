# SPDX-License-Identifier: GPL-2.0-or-later
"""Local import of decrypted PS4 save exports, with backup and rollback."""
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import tempfile
import zipfile

from prepare import sfo
import save_backup

MAX_BYTES = 128 * 1024 * 1024


@contextmanager
def open_source(source):
    source = Path(source)
    if source.is_dir():
        yield source
        return
    if source.suffix.lower() != '.zip':
        raise ValueError('Choose a decrypted save folder or ZIP archive.')
    with tempfile.TemporaryDirectory(prefix='gow3-save-import-') as temporary:
        root = Path(temporary)
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > 2048 or sum(e.file_size for e in entries) > MAX_BYTES:
                raise ValueError('Save archive is too large.')
            destinations = set()
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if (path.is_absolute() or '..' in path.parts or '\\' in entry.filename
                        or any(':' in part for part in path.parts)
                        or stat.S_ISLNK(entry.external_attr >> 16)):
                    raise ValueError('Unsafe path in save archive.')
                target = root.joinpath(*path.parts)
                # Windows filenames ignore trailing spaces/dots; avoid aliases.
                key = '/'.join(p.rstrip(' .').casefold() for p in path.parts)
                if not key or key in destinations or any(p != p.rstrip(' .') for p in path.parts):
                    raise ValueError('Duplicate or invalid path in save archive.')
                destinations.add(key)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as incoming, target.open('xb') as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
        yield root


def _text(value, size):
    return str(value).encode('utf-8')[:size - 1].decode('utf-8', 'ignore').encode('utf-8')


def inspect(root, title_id):
    if not re.fullmatch(r'CUSA\d{5}', title_id or ''):
        raise ValueError('Select your game PKG or folder before importing saves.')
    results, names = [], set()
    for metadata in Path(root).rglob('param.sfo'):
        if metadata.parent.name != 'sce_sys':
            continue
        if metadata.stat().st_size > 65536:
            raise ValueError('Save metadata is too large.')
        values = sfo(metadata.read_bytes())
        if values.get('TITLE_ID') != title_id:
            continue
        name = values.get('SAVEDATA_DIRECTORY', '')
        if not re.fullmatch(r'[A-Za-z0-9_@-]{1,31}', name):
            raise ValueError('Invalid save directory name.')
        if name.casefold() in names:
            raise ValueError('Duplicate save directory: ' + name)
        directory = metadata.parent.parent
        files, total = [], 0
        for path in directory.rglob('*'):
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                raise ValueError('Save folders must not contain links.')
            relative = path.relative_to(directory)
            if relative.parts[0] == 'sce_sys' or not path.is_file():
                continue
            total += path.stat().st_size
            if total > MAX_BYTES:
                raise ValueError('Save payload is too large.')
            files.append((path, relative))
        if not files:
            raise ValueError('Missing decrypted save payload: ' + name)
        names.add(name.casefold())
        results.append((name, directory, values, files))
    if not results:
        if any(p.name.startswith('sdimg_') for p in Path(root).rglob('*')):
            raise ValueError('This is an encrypted console backup. Decrypt it with a compatible save tool first; the PC port cannot decrypt sealed console keys offline.')
        raise ValueError('No decrypted saves for ' + title_id + ' found. The export must contain sce_sys/param.sfo and the game save files.')
    return results


def import_saves(source, user_dir, title_id):
    user_dir = Path(user_dir).resolve()
    user_dir.mkdir(parents=True, exist_ok=True)
    destination = user_dir / 'savedata/1' / title_id
    with open_source(source) as root:
        saves = inspect(root, title_id)  # validate every save before touching existing data
        if destination.resolve() != destination:
            raise ValueError('Save destination must not be a link.')
        with tempfile.TemporaryDirectory(prefix='save-import-', dir=user_dir) as temporary:
            staged = Path(temporary) / 'prepared'
            if destination.exists():
                if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in destination.rglob('*')):
                    raise ValueError('Existing save directories must not contain links.')
                shutil.copytree(destination, staged)
            else:
                staged.mkdir()
            for name, directory, values, files in saves:
                target = staged / name
                if target.exists():
                    shutil.rmtree(target)
                target.mkdir()
                for path, relative in files:
                    output = target / relative
                    output.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, output)
                meta = staged / (name + '.sce_sys')
                meta.mkdir(exist_ok=True)
                mtime = int(max(path.stat().st_mtime for path, _ in files))
                native = struct.pack('<128s128s1024sIi q32s',
                                     _text(values.get('MAINTITLE', ''), 128),
                                     _text(values.get('SUBTITLE', ''), 128),
                                     _text(values.get('DETAIL', ''), 1024),
                                     values.get('SAVEDATA_LIST_PARAM', 0), 0, mtime, bytes(32))
                (meta / 'param.bin').write_bytes(native)
                icon = directory / 'sce_sys/icon0.png'
                if icon.is_file():
                    shutil.copy2(icon, meta / 'icon0.png')
            backup = save_backup.backup(user_dir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            previous = Path(temporary) / 'previous'
            existed = destination.exists()
            if existed:
                destination.rename(previous)
            try:
                staged.rename(destination)
            except OSError:
                if existed:
                    previous.rename(destination)
                raise
    return {'count': len(saves), 'destination': str(destination), 'backup': str(backup) if backup else None}
