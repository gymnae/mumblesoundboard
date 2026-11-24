"""Writes tags, search tags and cover art into the sound files themselves.

Everything lives in the file, so a library copied elsewhere keeps it. Every
change is made on a copy that then atomically replaces the original: ffmpeg
processes still playing the old file keep reading the old inode undisturbed,
and a failed write never leaves a half-written sound behind.
"""
import base64
import io
import os
import re
import shutil
import tempfile

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TXXX
from mutagen.mp4 import MP4Cover, MP4FreeForm, MP4Tags
from PIL import Image, ImageOps

from sound_metadata import FRONT_COVER, MP4_TAGS_KEY, TAGS_FIELD, normalize_tags

TEXT_FIELDS = ('title', 'artist', 'album', 'year', 'genre')
MAX_FIELD_LENGTH = 200
YEAR_PATTERN = re.compile(r'^\d{4}(-\d{2}(-\d{2})?)?$')

ID3_FRAMES = {'title': TIT2, 'artist': TPE1, 'album': TALB, 'year': TDRC, 'genre': TCON}
MP4_KEYS = {'title': '\xa9nam', 'artist': '\xa9ART', 'album': '\xa9alb', 'year': '\xa9day', 'genre': '\xa9gen'}
VORBIS_KEYS = {'title': 'title', 'artist': 'artist', 'album': 'album', 'year': 'date', 'genre': 'genre'}

MAX_COVER_INPUT_BYTES = 15 * 1024 * 1024
COVER_MAX_PX = 1000


class TagWriteError(ValueError):
    """The request cannot be applied (bad input or an unsupported file)."""


def validate_fields(data):
    """Checks a {field: value} update. Returns cleaned values: str, or None to remove.
    Fields that are absent stay unchanged; 'tags' becomes a normalized list."""
    if not isinstance(data, dict):
        raise TagWriteError('expected a JSON object')
    unknown = set(data) - set(TEXT_FIELDS) - {'tags'}
    if unknown:
        raise TagWriteError('unknown field(s): ' + ', '.join(sorted(unknown)))
    cleaned = {}
    for field in TEXT_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if value is None:
            cleaned[field] = None
            continue
        if not isinstance(value, (str, int)):
            raise TagWriteError(f'{field} must be text')
        value = ' '.join(str(value).split())
        if len(value) > MAX_FIELD_LENGTH:
            raise TagWriteError(f'{field} is longer than {MAX_FIELD_LENGTH} characters')
        if field == 'year' and value and not YEAR_PATTERN.match(value):
            raise TagWriteError('year must look like 1998, 1998-05 or 1998-05-17')
        cleaned[field] = value or None
    if 'tags' in data:
        tags = data['tags']
        if tags is None:
            tags = []
        elif isinstance(tags, str):
            tags = [tags]
        elif not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise TagWriteError('tags must be a list of strings')
        cleaned['tags'] = normalize_tags(tags)
    return cleaned


def prepare_cover(data):
    """Validate an uploaded image and shrink it to at most COVER_MAX_PX.
    Returns (mime, bytes, width, height). Opaque images become JPEG, others PNG."""
    if not data:
        raise TagWriteError('empty image')
    if len(data) > MAX_COVER_INPUT_BYTES:
        raise TagWriteError('image is larger than 15 MB')
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        raise TagWriteError('not a readable image (use JPEG, PNG, GIF or WebP)')
    image = ImageOps.exif_transpose(image)
    alpha = image.mode in ('RGBA', 'LA', 'PA') or 'transparency' in image.info
    image = image.convert('RGBA' if alpha else 'RGB')
    image.thumbnail((COVER_MAX_PX, COVER_MAX_PX), Image.LANCZOS)
    buffer = io.BytesIO()
    if alpha:
        image.save(buffer, 'PNG', optimize=True)
        mime = 'image/png'
    else:
        image.save(buffer, 'JPEG', quality=90, optimize=True)
        mime = 'image/jpeg'
    return mime, buffer.getvalue(), image.width, image.height


def _open(path):
    try:
        audio = mutagen.File(path)
    except Exception as e:
        raise TagWriteError(f'cannot read tags: {e}')
    if audio is None:
        raise TagWriteError('unsupported file type for tag editing')
    if audio.tags is None:
        try:
            audio.add_tags()
        except Exception as e:
            raise TagWriteError(f'cannot add tags to this file: {e}')
    return audio


def _kind(audio):
    if isinstance(audio.tags, ID3):
        return 'id3'
    if isinstance(audio.tags, MP4Tags):
        return 'mp4'
    if hasattr(audio.tags, 'as_dict'):  # Vorbis comments: Ogg Vorbis/Opus/FLAC
        return 'vorbis'
    raise TagWriteError(f'tag editing is not supported for {type(audio).__name__} files')


def _apply_fields(audio, kind, fields):
    tags = audio.tags
    for field, value in fields.items():
        if field == 'tags':
            continue
        if kind == 'id3':
            frame = ID3_FRAMES[field]
            tags.delall(frame.__name__)
            if value:
                tags.add(frame(encoding=3, text=[value]))
        elif kind == 'mp4':
            key = MP4_KEYS[field]
            if value:
                tags[key] = [value]
            elif key in tags:
                del tags[key]
        else:
            key = VORBIS_KEYS[field]
            if value:
                tags[key] = [value]
            elif key in tags:
                del tags[key]
    if 'tags' in fields:
        values = fields['tags']
        if kind == 'id3':
            for frame in list(tags.getall('TXXX')):
                if frame.desc.upper() == TAGS_FIELD:
                    tags.delall(frame.HashKey)
            if values:
                tags.add(TXXX(encoding=3, desc=TAGS_FIELD, text=values))
        elif kind == 'mp4':
            if values:
                tags[MP4_TAGS_KEY] = [MP4FreeForm(v.encode('utf-8')) for v in values]
            elif MP4_TAGS_KEY in tags:
                del tags[MP4_TAGS_KEY]
        else:
            key = TAGS_FIELD.lower()
            if values:
                tags[key] = values
            elif key in tags:
                del tags[key]


def _apply_cover(audio, kind, cover):
    """cover: (mime, bytes, width, height) or None to remove all embedded pictures."""
    tags = audio.tags
    if kind == 'id3':
        tags.delall('APIC')
        if cover:
            tags.add(APIC(encoding=3, mime=cover[0], type=FRONT_COVER, desc='Cover', data=cover[1]))
    elif kind == 'mp4':
        if cover:
            fmt = MP4Cover.FORMAT_PNG if cover[0] == 'image/png' else MP4Cover.FORMAT_JPEG
            tags['covr'] = [MP4Cover(cover[1], imageformat=fmt)]
        elif 'covr' in tags:
            del tags['covr']
    else:
        for key in ('metadata_block_picture', 'coverart', 'coverartmime'):
            if key in tags:
                del tags[key]
        if isinstance(audio, FLAC):
            audio.clear_pictures()
        if cover:
            picture = Picture()
            picture.type = FRONT_COVER
            picture.mime = cover[0]
            picture.desc = 'Cover'
            picture.data = cover[1]
            picture.width, picture.height = cover[2], cover[3]
            picture.depth = 32 if cover[0] == 'image/png' else 24
            if isinstance(audio, FLAC):
                audio.add_picture(picture)
            else:
                tags['metadata_block_picture'] = [base64.b64encode(picture.write()).decode('ascii')]


def _edit_atomically(path, change):
    """Run change(audio, kind) on a copy of path, then replace path with it."""
    directory = os.path.dirname(path)
    # Same directory = same filesystem, so os.replace is atomic. The name starts
    # with a dot and keeps the real extension (mutagen sniffs by it).
    fd, tmp = tempfile.mkstemp(prefix='.edit-', suffix=os.path.splitext(path)[1], dir=directory)
    os.close(fd)
    try:
        shutil.copy2(path, tmp)
        audio = _open(tmp)
        change(audio, _kind(audio))
        audio.save()
        with open(tmp, 'rb+') as f:
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except TagWriteError:
        raise
    except Exception as e:
        raise TagWriteError(f'could not write tags: {e}')
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_fields(path, fields):
    """Apply validated fields (see validate_fields) to the file at path."""
    if not fields:
        return
    _edit_atomically(path, lambda audio, kind: _apply_fields(audio, kind, fields))


def write_cover(path, image_bytes):
    """Embed image_bytes as the front cover, or remove all pictures when None."""
    cover = prepare_cover(image_bytes) if image_bytes is not None else None
    _edit_atomically(path, lambda audio, kind: _apply_cover(audio, kind, cover))
