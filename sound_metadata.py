"""Tag and cover-art extraction for files in the sounds directory.

Uses mutagen, which only reads the container headers, so scanning a large
library is cheap. Results are cached per file and invalidated when the file's
mtime or size changes.
"""
import base64
import os
import threading

import mutagen
from mutagen.flac import Picture
from mutagen.id3 import ID3
from mutagen.mp4 import MP4Cover

# Front cover first, then whatever else is embedded (ID3/FLAC picture types).
FRONT_COVER = 3

# Common tag names per container: ID3 frames, MP4 atoms, Vorbis comments.
TEXT_TAGS = {
    'title': ('TIT2', '\xa9nam', 'title'),
    'artist': ('TPE1', '\xa9ART', 'artist'),
    'album': ('TALB', '\xa9alb', 'album'),
    'year': ('TDRC', '\xa9day', 'date'),
    'genre': ('TCON', '\xa9gen', 'genre'),
}


def _first_text(tags, keys):
    for key in keys:
        try:
            value = tags.get(key)
        except (KeyError, ValueError):
            continue
        if value is None:
            continue
        # ID3 frames carry .text (TCON resolves numeric genres via .genres),
        # MP4/Vorbis return plain lists.
        items = getattr(value, 'genres', None) or getattr(value, 'text', value)
        if not isinstance(items, (list, tuple)):
            items = [items]
        for item in items:
            text = str(item).strip()
            if text:
                return text
    return None


def _sniff_mime(data):
    if data.startswith(b'\x89PNG'):
        return 'image/png'
    if data.startswith(b'GIF8'):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/jpeg'


def _pick_picture(pictures):
    """pictures: iterable of (picture_type, mime, data). Returns (mime, data) or None."""
    pictures = [p for p in pictures if p[2]]
    if not pictures:
        return None
    pictures.sort(key=lambda p: p[0] != FRONT_COVER)
    _, mime, data = pictures[0]
    if not mime or not mime.startswith('image/'):
        mime = _sniff_mime(data)
    return mime, data


def _extract_cover(audio):
    tags = audio.tags
    # FLAC stores pictures outside the comment block.
    pictures = [(p.type, p.mime, p.data) for p in getattr(audio, 'pictures', None) or []]

    if isinstance(tags, ID3):
        pictures += [(f.type, f.mime, f.data) for f in tags.getall('APIC')]
    elif tags is not None:
        for cover in tags.get('covr', None) or []:
            mime = 'image/png' if getattr(cover, 'imageformat', None) == MP4Cover.FORMAT_PNG else 'image/jpeg'
            pictures.append((FRONT_COVER, mime, bytes(cover)))
        # Ogg Vorbis/Opus embed base64 FLAC picture blocks in a comment.
        for block in tags.get('metadata_block_picture', None) or []:
            try:
                p = Picture(base64.b64decode(block))
                pictures.append((p.type, p.mime, p.data))
            except Exception:
                continue

    return _pick_picture(pictures)


def read_file(path):
    """Parse one file. Returns (metadata dict, cover tuple or None)."""
    meta = {key: None for key in TEXT_TAGS}
    meta['duration'] = None
    try:
        audio = mutagen.File(path)
    except Exception:
        audio = None
    if audio is None:
        return meta, None

    length = getattr(getattr(audio, 'info', None), 'length', None)
    if length:
        meta['duration'] = round(float(length), 2)

    if audio.tags is not None:
        for key, names in TEXT_TAGS.items():
            meta[key] = _first_text(audio.tags, names)

    try:
        cover = _extract_cover(audio)
    except Exception:
        cover = None
    return meta, cover


class MetadataCache:
    """Caches tags (always) and cover bytes (on demand) keyed by path + mtime/size."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}

    @staticmethod
    def _stamp(path):
        st = os.stat(path)
        return st.st_mtime_ns, st.st_size

    def _load(self, path):
        stamp = self._stamp(path)
        with self._lock:
            entry = self._entries.get(path)
            if entry and entry['stamp'] == stamp:
                return entry
        meta, cover = read_file(path)
        entry = {
            'stamp': stamp,
            'meta': meta,
            'has_cover': cover is not None,
        }
        with self._lock:
            self._entries[path] = entry
        return entry

    def metadata(self, path):
        """Tag dict plus has_cover. Raises OSError if the file is gone."""
        entry = self._load(path)
        return dict(entry['meta'], has_cover=entry['has_cover'])

    def cover(self, path):
        """(mime, bytes, stamp) for the embedded cover, or None.

        Cover bytes are not cached: they can be large, and browsers cache them
        via the ETag the caller builds from stamp.
        """
        entry = self._load(path)
        if not entry['has_cover']:
            return None
        _, cover = read_file(path)
        if cover is None:
            return None
        return cover[0], cover[1], entry['stamp']
