#!/usr/bin/env python3
"""Bounded aria2 magnet downloader producing one Telegram-ready artifact."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

from archive_processor import clean_delivery_name
from artifacts import InvalidMagnet, magnet_info_hash


class TorrentDownloadError(RuntimeError):
    pass


class TorrentTooLarge(TorrentDownloadError):
    pass


def _payload_files(root: Path) -> list[Path]:
    return [p for p in root.rglob('*')
            if p.is_file() and not p.is_symlink()
            and p.suffix.lower() not in ('.aria2', '.torrent')]


def _payload_size(root: Path) -> int:
    return sum(p.stat().st_size for p in _payload_files(root))


def _terminate(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=5)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return 'sha256:' + digest.hexdigest()


def download_magnet(magnet: str, destination_dir: str, title: str, *, progress=None,
                    max_bytes: int = 2_000_000_000,
                    reserve_bytes: int = 1_073_741_824,
                    timeout: int = 21_600, aria2_path: str = 'aria2c'):
    magnet_info_hash(magnet)
    if max_bytes <= 0 or reserve_bytes < 0 or timeout <= 0:
        raise ValueError('invalid torrent limits')
    executable = shutil.which(aria2_path) if os.sep not in aria2_path else aria2_path
    if not executable or not Path(executable).is_file():
        raise TorrentDownloadError('aria2c is not installed')
    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)
    torrent_root = destination / 'torrent'
    torrent_root.mkdir()
    if shutil.disk_usage(destination).free < reserve_bytes:
        raise TorrentDownloadError('insufficient disk reserve')
    command = [
        executable, f'--dir={torrent_root}', '--seed-time=0',
        '--file-allocation=none', '--allow-overwrite=false',
        '--auto-file-renaming=false', '--summary-interval=0',
        '--console-log-level=warn', '--download-result=hide',
        '--max-connection-per-server=8', str(magnet),
    ]
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    started = time.monotonic()
    try:
        while proc.poll() is None:
            size = _payload_size(torrent_root)
            if size > max_bytes:
                raise TorrentTooLarge(f'torrent exceeds limit: {max_bytes} bytes')
            if shutil.disk_usage(destination).free < reserve_bytes:
                raise TorrentDownloadError('disk reserve reached during torrent download')
            if time.monotonic() - started > timeout:
                raise TorrentDownloadError('torrent download timed out')
            if progress:
                progress(size, None)
            time.sleep(1)
        size = _payload_size(torrent_root)
        if size > max_bytes:
            raise TorrentTooLarge(f'torrent exceeds limit: {max_bytes} bytes')
        if proc.returncode != 0:
            raise TorrentDownloadError(f'aria2c failed with exit code {proc.returncode}')
    except Exception:
        _terminate(proc)
        raise

    files = _payload_files(torrent_root)
    if not files:
        raise TorrentDownloadError('torrent completed without payload files')
    safe_title = clean_delivery_name(title)
    if len(files) == 1:
        source = files[0]
        suffix = source.suffix.lower()[:16]
        output = destination / (safe_title + suffix)
        os.replace(source, output)
    else:
        total_payload = sum(source.stat().st_size for source in files)
        if shutil.disk_usage(destination).free < total_payload + reserve_bytes:
            raise TorrentDownloadError('insufficient disk space to package torrent')
        output = destination / (safe_title + '.zip')
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED,
                             allowZip64=True) as archive:
            for source in sorted(files):
                archive.write(source, source.relative_to(torrent_root).as_posix())
        if output.stat().st_size > max_bytes:
            output.unlink(missing_ok=True)
            raise TorrentTooLarge(f'packed torrent exceeds limit: {max_bytes} bytes')
        with zipfile.ZipFile(output) as archive:
            bad = archive.testzip()
            if bad:
                output.unlink(missing_ok=True)
                raise TorrentDownloadError(f'torrent ZIP failed CRC at {bad}')
    return {'path': str(output), 'file_size': output.stat().st_size,
            'checksum': _sha256(output), 'final_url': str(magnet)}


_VIDEO_SUFFIXES = {
    '.3gp', '.avi', '.flv', '.m4v', '.mkv', '.mov', '.mp4', '.mpeg',
    '.mpg', '.mts', '.ts', '.webm', '.wmv',
}


def _bdecode(data: bytes):
    def parse(pos):
        if pos >= len(data):
            raise TorrentDownloadError('truncated torrent metadata')
        marker = data[pos:pos + 1]
        if marker == b'i':
            end = data.index(b'e', pos + 1)
            return int(data[pos + 1:end]), end + 1
        if marker == b'l':
            values, pos = [], pos + 1
            while data[pos:pos + 1] != b'e':
                value, pos = parse(pos)
                values.append(value)
            return values, pos + 1
        if marker == b'd':
            values, pos = {}, pos + 1
            while data[pos:pos + 1] != b'e':
                key, pos = parse(pos)
                value, pos = parse(pos)
                values[key] = value
            return values, pos + 1
        if marker.isdigit():
            colon = data.index(b':', pos)
            length = int(data[pos:colon])
            start = colon + 1
            end = start + length
            if end > len(data):
                raise TorrentDownloadError('truncated torrent string')
            return data[start:end], end
        raise TorrentDownloadError('invalid bencoded torrent metadata')

    value, end = parse(0)
    if end != len(data):
        raise TorrentDownloadError('trailing bytes in torrent metadata')
    return value


def _bencode(value) -> bytes:
    if isinstance(value, int):
        return b'i' + str(value).encode('ascii') + b'e'
    if isinstance(value, bytes):
        return str(len(value)).encode('ascii') + b':' + value
    if isinstance(value, list):
        return b'l' + b''.join(_bencode(item) for item in value) + b'e'
    if isinstance(value, dict):
        return b'd' + b''.join(_bencode(key) + _bencode(value[key])
                               for key in sorted(value)) + b'e'
    raise TorrentDownloadError('unsupported value in torrent metadata')


def _torrent_file_entries(torrent_path: Path, expected_hash: str):
    metadata = _bdecode(torrent_path.read_bytes())
    info = metadata.get(b'info') if isinstance(metadata, dict) else None
    if not isinstance(info, dict) or hashlib.sha1(_bencode(info)).hexdigest() != expected_hash:
        raise TorrentDownloadError('torrent metadata does not match magnet info hash')

    def decode_component(value):
        if not isinstance(value, bytes):
            raise TorrentDownloadError('invalid torrent file path')
        text = value.decode('utf-8', errors='replace').replace('\\', '/')
        if not text or text in ('.', '..') or '/' in text:
            raise TorrentDownloadError('unsafe torrent file path component')
        return text

    entries = []
    files = info.get(b'files')
    if files is None:
        length = info.get(b'length')
        name = info.get(b'name.utf-8', info.get(b'name'))
        if not isinstance(length, int) or length < 0:
            raise TorrentDownloadError('invalid single-file torrent length')
        entries.append((1, (decode_component(name),), length))
    else:
        if not isinstance(files, list):
            raise TorrentDownloadError('invalid multi-file torrent metadata')
        root_name = decode_component(info.get(b'name.utf-8', info.get(b'name')))
        for index, entry in enumerate(files, 1):
            if not isinstance(entry, dict):
                raise TorrentDownloadError('invalid torrent file entry')
            length = entry.get(b'length')
            parts = entry.get(b'path.utf-8', entry.get(b'path'))
            if not isinstance(length, int) or length < 0 or not isinstance(parts, list):
                raise TorrentDownloadError('invalid torrent file metadata')
            entries.append((index, (root_name,) + tuple(decode_component(x) for x in parts), length))
    return entries


def _run_aria2_download(command, payload_root: Path, *, max_bytes: int,
                        reserve_bytes: int, timeout: int, progress=None):
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = time.monotonic()
    try:
        while proc.poll() is None:
            size = _payload_size(payload_root)
            if size > max_bytes:
                raise TorrentTooLarge(f'torrent exceeds limit: {max_bytes} bytes')
            if shutil.disk_usage(payload_root).free < reserve_bytes:
                raise TorrentDownloadError('disk reserve reached during torrent download')
            if time.monotonic() - started > timeout:
                raise TorrentDownloadError('torrent download timed out')
            if progress:
                progress(size, None)
            time.sleep(1)
        size = _payload_size(payload_root)
        if size > max_bytes:
            raise TorrentTooLarge(f'torrent exceeds limit: {max_bytes} bytes')
        if proc.returncode != 0:
            raise TorrentDownloadError(f'aria2c failed with exit code {proc.returncode}')
    except Exception:
        _terminate(proc)
        raise


def download_video_magnet(magnet: str, destination_dir: str, title: str, *, progress=None,
                          max_bytes: int = 2_000_000_000,
                          reserve_bytes: int = 1_073_741_824,
                          timeout: int = 21_600, aria2_path: str = 'aria2c'):
    """Fetch torrent metadata, then download exactly one largest eligible video file."""
    expected_hash = magnet_info_hash(magnet)
    if max_bytes <= 0 or reserve_bytes < 0 or timeout <= 0:
        raise ValueError('invalid torrent limits')
    executable = shutil.which(aria2_path) if os.sep not in aria2_path else aria2_path
    if not executable or not Path(executable).is_file():
        raise TorrentDownloadError('aria2c is not installed')
    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)
    torrent_root = destination / 'torrent'
    torrent_root.mkdir()
    if shutil.disk_usage(destination).free < reserve_bytes:
        raise TorrentDownloadError('insufficient disk reserve')

    common = [executable, f'--dir={torrent_root}', '--seed-time=0',
              '--file-allocation=none', '--allow-overwrite=false',
              '--auto-file-renaming=false', '--summary-interval=0',
              '--console-log-level=warn', '--download-result=hide',
              '--max-connection-per-server=8']
    metadata_command = common + ['--bt-metadata-only=true', '--bt-save-metadata=true', magnet]
    _run_aria2_download(metadata_command, torrent_root, max_bytes=max_bytes,
                        reserve_bytes=reserve_bytes, timeout=timeout, progress=progress)
    torrent_files = sorted(torrent_root.glob('*.torrent'))
    if not torrent_files:
        raise TorrentDownloadError('aria2c completed without saving torrent metadata')
    entries = _torrent_file_entries(torrent_files[0], expected_hash)
    candidates = [entry for entry in entries
                  if Path(entry[1][-1]).suffix.lower() in _VIDEO_SUFFIXES and 0 < entry[2] <= max_bytes]
    if not candidates:
        if any(Path(entry[1][-1]).suffix.lower() in _VIDEO_SUFFIXES and entry[2] > max_bytes
               for entry in entries):
            raise TorrentTooLarge(f'no video file fits size limit: {max_bytes} bytes')
        raise TorrentDownloadError('torrent contains no eligible video file')
    index, relative_parts, expected_size = sorted(candidates, key=lambda item: (-item[2], item[0]))[0]
    selected_command = common + [f'--select-file={index}', str(torrent_files[0])]
    _run_aria2_download(selected_command, torrent_root, max_bytes=max_bytes,
                        reserve_bytes=reserve_bytes, timeout=timeout, progress=progress)
    source = torrent_root.joinpath(*relative_parts)
    resolved_root = torrent_root.resolve()
    if source.is_symlink() or not source.is_file() or resolved_root not in source.resolve().parents:
        raise TorrentDownloadError('selected video file was not downloaded safely')
    actual_size = source.stat().st_size
    if actual_size <= 0 or actual_size > max_bytes:
        raise TorrentTooLarge(f'selected video has invalid size: {actual_size} bytes')
    suffix = source.suffix.lower()
    safe_title = clean_delivery_name(title)
    output = destination / (safe_title + suffix)
    os.replace(source, output)
    return {'path': str(output), 'file_size': output.stat().st_size,
            'checksum': _sha256(output), 'final_url': str(magnet),
            'selected_file_index': index}


__all__ = ['InvalidMagnet', 'TorrentDownloadError', 'TorrentTooLarge',
           'download_magnet', 'download_video_magnet', 'magnet_info_hash']
