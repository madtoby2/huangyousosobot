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


__all__ = ['InvalidMagnet', 'TorrentDownloadError', 'TorrentTooLarge',
           'download_magnet', 'magnet_info_hash']
