import struct
import tempfile
import unittest
from pathlib import Path
import zipfile
from unittest.mock import patch

from paths import ROOT
from save_import import import_saves


def fixture(root, title='CUSA01715', name='AUTOSAVE'):
    directory = root / title / name
    (directory / 'sce_sys').mkdir(parents=True)
    fields = {'TITLE_ID': title, 'SAVEDATA_DIRECTORY': name, 'MAINTITLE': 'God of War III',
              'SUBTITLE': 'Upper Chamber', 'DETAIL': 'Play time: 3:06:19'}
    keys, values, entries = bytearray(), bytearray(), []
    for key, value in fields.items():
        value = value.encode() + b'\0'
        entries.append((len(keys), 0x204, len(value), len(value), len(values)))
        keys.extend(key.encode() + b'\0')
        values.extend(value)
    offset = 20 + 16 * len(entries)
    data = struct.pack('<4sIIII', b'\0PSF', 0x101, offset, offset + len(keys), len(entries))
    data += b''.join(struct.pack('<HHIII', *entry) for entry in entries) + keys + values
    (directory / 'sce_sys/param.sfo').write_bytes(data)
    (directory / 'SAVEDATA').write_bytes(b'original console payload')
    return directory


class ImportTests(unittest.TestCase):
    def test_import_creates_native_metadata_and_backs_up_existing_slot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = fixture(root / 'export')
            current = root / 'user/savedata/1/CUSA01715/AUTOSAVE'
            current.mkdir(parents=True)
            (current / 'SAVEDATA').write_bytes(b'previous PC save')
            result = import_saves(root / 'export', root / 'user', 'CUSA01715')
            self.assertEqual(result['count'], 1)
            self.assertEqual((current / 'SAVEDATA').read_bytes(), (source / 'SAVEDATA').read_bytes())
            self.assertEqual((Path(result['backup']) / '1/CUSA01715/AUTOSAVE/SAVEDATA').read_bytes(), b'previous PC save')
            native = (current.parent / 'AUTOSAVE.sce_sys/param.bin').read_bytes()
            self.assertEqual(len(native), 1328)
            self.assertEqual(native[128:256].rstrip(b'\0'), b'Upper Chamber')
            self.assertFalse((current / 'sce_sys').exists())

    def test_zip_import_and_title_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root / 'export')
            archive = root / 'save.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                for file in (root / 'export').rglob('*'):
                    if file.is_file():
                        output.write(file, file.relative_to(root / 'export').as_posix())
            self.assertEqual(import_saves(archive, root / 'user', 'CUSA01715')['count'], 1)
            with self.assertRaisesRegex(ValueError, 'No decrypted saves'):
                import_saves(archive, root / 'other-user', 'CUSA01623')

    def test_unsafe_archive_rejected_before_changing_saves(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / 'save.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('../escape', b'bad')
            with self.assertRaisesRegex(ValueError, 'Unsafe path'):
                import_saves(archive, root / 'user', 'CUSA01715')
            self.assertFalse((root / 'user/savedata').exists())

    def test_encrypted_backup_explains_decryption_requirement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'sdimg_AUTOSAVE').write_bytes(b'encrypted')
            with self.assertRaisesRegex(ValueError, 'encrypted console backup'):
                import_saves(root, root / 'user', 'CUSA01715')

    def test_failed_install_restores_existing_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root / 'export')
            current = root / 'user/savedata/1/CUSA01715/AUTOSAVE'
            current.mkdir(parents=True)
            (current / 'SAVEDATA').write_bytes(b'previous PC save')
            rename = Path.rename
            def fail_staged(path, target):
                if path.name == 'prepared':
                    raise OSError('simulated install failure')
                return rename(path, target)
            with patch.object(Path, 'rename', fail_staged):
                with self.assertRaisesRegex(OSError, 'simulated'):
                    import_saves(root / 'export', root / 'user', 'CUSA01715')
            self.assertEqual((current / 'SAVEDATA').read_bytes(), b'previous PC save')
