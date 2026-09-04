#!/usr/bin/env python3
"""Safely remove source archive passwords and produce a verified plain ZIP."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


class ArchiveProcessingError(RuntimeError):
    pass


class UnsupportedArchive(ArchiveProcessingError):
    pass


class ArchivePasswordError(ArchiveProcessingError):
    pass


class ArchiveDiskSpaceError(ArchiveProcessingError):
    pass


class ArchiveVerificationError(ArchiveProcessingError):
    pass


SUPPORTED_SUFFIXES = {'.rar', '.zip', '.7z'}
AD_SUFFIXES = {'.url', '.webloc', '.desktop', '.website'}
DEFAULT_SOURCE_PASSWORDS = {
    'ryuugames': ['ryuugames.com', 'www.ryuugames.com'],
    'otomi': ['otomi-games.com', 'www.otomi-games.com'],
}


def passwords_for_source(source: str, environ=None) -> list[str]:
    environ = os.environ if environ is None else environ
    normalized = (source or '').strip().lower()
    key = 'RYUUGAMES_ARCHIVE_PASSWORDS' if normalized == 'ryuugames' else 'OTOMI_ARCHIVE_PASSWORDS'
    configured = str(environ.get(key, '')).strip()
    if configured:
        return [item.strip() for item in configured.split(',') if item.strip()]
    return list(DEFAULT_SOURCE_PASSWORDS.get(normalized, []))
SPACE_ERRORS = ('no space left', 'disk full', 'not enough space', 'enospc',
                'cannot write', 'insufficient disk', 'no space on device')


def clean_delivery_name(value: str) -> str:
    name = str(value or '').strip()
    name = re.sub(r'(?i)(?:https?://)?(?:www\.)?ryu+games(?:\.com)?|sukebei|nyaa|javdb', ' ', name)
    name = re.sub(r'(?i)\bdecrypted\b', ' ', name)
    name = name.replace('已解密', ' ')
    name = re.sub(r'(?i)\.(?:rar|zip|7z)$', '', name.strip())
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', ' ', name)
    name = re.sub(r'[\[\]{}()【】]+', ' ', name)
    name = re.sub(r'[_\s]+', ' ', name).strip(' ._-') or 'game'
    encoded = name.encode('utf-8')
    while len(encoded) > 180:
        name = name[:-1]
        encoded = name.encode('utf-8')
    return name.rstrip(' ._-') or 'game'


def _listed_unpacked_size(path: Path) -> int:
    proc = subprocess.run(['7z', 'l', '-slt', str(path)], capture_output=True,
                          text=True, timeout=120)
    if proc.returncode != 0:
        raise ArchiveProcessingError('archive metadata cannot be read')
    listing = proc.stdout.split('----------', 1)[-1]
    sizes = [int(match.group(1)) for match in
             re.finditer(r'^Size = (\d+)$', listing, re.MULTILINE)]
    if not sizes:
        raise ArchiveProcessingError('archive contains no readable files')
    return sum(sizes)


def _check_space(path: Path, unpacked_size: int, reserve_bytes: int):
    required = unpacked_size * 2 + max(0, int(reserve_bytes))
    if shutil.disk_usage(path.parent).free < required:
        raise ArchiveDiskSpaceError(
            f'insufficient disk space for extraction and repack: need {required} bytes')


def _extract_command(source: Path, destination: Path, password: str):
    if source.suffix.lower() == '.rar' and shutil.which('unrar'):
        password_arg = '-p-' if not password else f'-p{password}'
        return ['unrar', 'x', '-idq', '-o+', password_arg,
                str(source), str(destination) + os.sep]
    password_arg = '-p' if not password else f'-p{password}'
    return ['7z', 'x', '-y', password_arg, f'-o{destination}', str(source)]


def _extract(source: Path, destination: Path, passwords: list[str]):
    candidates = []
    for password in ['', *(passwords or [])]:
        password = str(password)
        if password not in candidates:
            candidates.append(password)
    last_output = ''
    for password in candidates:
        shutil.rmtree(destination, ignore_errors=True)
        destination.mkdir(parents=True)
        proc = subprocess.run(_extract_command(source, destination, password),
                              capture_output=True, text=True, timeout=1800)
        output = ((proc.stdout or '') + '\n' + (proc.stderr or '')).strip()
        if proc.returncode == 0:
            return
        last_output = output
        lowered = output.lower()
        if any(marker in lowered for marker in SPACE_ERRORS):
            raise ArchiveDiskSpaceError('disk filled while extracting archive')
    raise ArchivePasswordError(
        'no configured source password could extract archive: ' + last_output[-240:])


def _clean_payload(root: Path):
    for path in sorted(root.rglob('*'), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.is_file():
            name = path.name
            if path.suffix.lower() in AD_SUFFIXES or re.search(r'ryuu', name, re.I):
                path.unlink()
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    files = [path for path in root.rglob('*') if path.is_file()]
    if not files:
        raise ArchiveProcessingError('archive has no payload after cleanup')
    return files


def _write_plain_zip(root: Path, output: Path):
    output.unlink(missing_ok=True)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED,
                         allowZip64=True) as archive:
        for path in sorted(root.rglob('*')):
            if path.is_file() and not path.is_symlink():
                archive.write(path, path.relative_to(root).as_posix())


def _verify_plain_zip(path: Path):
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos or any(info.flag_bits & 0x1 for info in infos):
                raise ArchiveVerificationError('output ZIP is empty or encrypted')
            bad = archive.testzip()
            if bad:
                raise ArchiveVerificationError(f'output ZIP failed CRC at {bad}')
    except ArchiveVerificationError:
        raise
    except Exception as exc:
        raise ArchiveVerificationError('output ZIP verification failed') from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return 'sha256:' + digest.hexdigest()


def prepare_archive(path: str, passwords: list[str], *, output_name: str | None = None,
                    reserve_bytes: int = 536_870_912):
    source = Path(path)
    if not source.is_file() or source.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise UnsupportedArchive('paid game delivery requires a RAR, ZIP or 7z archive')
    unpacked_size = _listed_unpacked_size(source)
    _check_space(source, unpacked_size, reserve_bytes)
    delivery_name = clean_delivery_name(output_name if output_name is not None else source.stem)
    output = source.with_name(delivery_name + '.zip')
    if output == source:
        output = source.with_name(delivery_name + ' (1).zip')
    work = Path(tempfile.mkdtemp(prefix='.decrypt-', dir=str(source.parent)))
    try:
        _extract(source, work, passwords)
        _clean_payload(work)
        _write_plain_zip(work, output)
        _verify_plain_zip(output)
        return {'path': str(output), 'file_size': output.stat().st_size,
                'checksum': _sha256(output)}
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
