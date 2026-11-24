"""Sound library maintenance: content fingerprints (duplicate detection),
loudness analysis (playback normalization), uploads and masked deletes.

Fingerprint
    SHA-256 over the file's compressed audio packets, taken with
    ``ffmpeg -c copy -f hash``. Tags and cover art are not audio packets, so
    editing them (here or in any tag editor) keeps the fingerprint; renaming a
    file keeps it too. Hashing does not decode, so it is fast enough to run
    synchronously before an upload is accepted.

Loudness
    EBU R128 integrated loudness and true peak, measured once per fingerprint by
    ffmpeg's loudnorm filter in a background thread. Playback then applies one
    static gain towards the target (plus a peak limiter only when that gain
    would push peaks over the ceiling). A static gain keeps the sound's own
    dynamics and adds no latency, unlike real-time loudnorm, which needs a 3 s
    look-ahead buffer before the first sample comes out.

Deleting
    Never removes data: the file is renamed to ``<name>.deleted-<UTC time>``.
    That name no longer ends in an allowed extension, so every listing and
    playback path ignores it, and it can be restored by renaming it back.
"""
import json
import math
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import unicodedata

DELETED_MARK = '.deleted-'
DELETED_PATTERN = re.compile(r'^(?P<name>.+)\.deleted-(?P<stamp>\d{8}T\d{6}Z)(?:-\d+)?$')
UPLOAD_PREFIX = '.upload-'
# Integrated loudness needs at least one 400 ms gating block; clips shorter
# than that are looped up to this many seconds before measuring.
SHORT_CLIP_LOOP_SECONDS = 3
SILENCE_LUFS = -70.0


class LibraryError(Exception):
    """A user-facing failure, with the HTTP status it maps to."""
    status = 400

    def __init__(self, message, status=None, **details):
        super().__init__(message)
        if status:
            self.status = status
        self.details = details


class DuplicateError(LibraryError):
    status = 409


# --- ffmpeg helpers -------------------------------------------------------

def audio_fingerprint(path, timeout=120):
    """'sha256:<hex>' of the first audio stream's packets, or None when ffmpeg
    finds no audio stream (not an audio file, or a broken one)."""
    try:
        result = subprocess.run(
            ['ffmpeg', '-nostdin', '-v', 'error', '-i', path, '-map', '0:a:0',
             '-c', 'copy', '-f', 'hash', '-hash', 'sha256', '-'],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r'SHA256=([0-9a-f]{64})', result.stdout)
    if result.returncode != 0 or not match:
        return None
    return 'sha256:' + match.group(1)


def decodes_cleanly(path, seconds=5, timeout=60):
    """True if ffmpeg can decode the first seconds of audio without errors."""
    try:
        result = subprocess.run(
            ['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', path, '-map', '0:a:0',
             '-t', str(seconds), '-f', 'null', '-'],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def render_preview(path, audio_filter=None, timeout=120):
    """MP3 bytes of the sound as the channel would play it (same filter), for
    listening in the browser. Raises LibraryError if ffmpeg fails."""
    cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-i', path, '-map', '0:a:0', '-vn']
    if audio_filter:
        cmd += ['-af', audio_filter]
    cmd += ['-c:a', 'libmp3lame', '-b:a', '192k', '-f', 'mp3', 'pipe:1']
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise LibraryError(f'could not render a preview: {e}', 500)
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode(errors='replace').strip().splitlines()[-1:] or ['no audio']
        raise LibraryError(f'could not render a preview: {detail[0]}', 500)
    return result.stdout


def _parse_db(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _loudnorm(path, loop_seconds=None, timeout=600):
    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-nostats']
    if loop_seconds:
        cmd += ['-stream_loop', '-1']
    cmd += ['-i', path, '-map', '0:a:0']
    if loop_seconds:
        cmd += ['-t', str(loop_seconds)]
    cmd += ['-af', 'loudnorm=print_format=json', '-f', 'null', '-']
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    # The JSON summary is the last {...} block on stderr.
    start = result.stderr.rfind('{')
    end = result.stderr.rfind('}')
    if result.returncode != 0 or start < 0 or end < start:
        raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'ffmpeg failed')
    data = json.loads(result.stderr[start:end + 1])
    return _parse_db(data.get('input_i')), _parse_db(data.get('input_tp'))


def measure_loudness(path):
    """(integrated LUFS or None if silent, true peak dBTP or None). Raises on failure."""
    lufs, peak = _loudnorm(path)
    if lufs is None and peak is not None:
        # Audible but too short for a gating block: measure it looped.
        lufs, peak = _loudnorm(path, loop_seconds=SHORT_CLIP_LOOP_SECONDS)
    if lufs is not None and lufs <= SILENCE_LUFS:
        lufs = None
    return lufs, peak


def normalization_gain(lufs, true_peak, target, max_gain_db, ceiling_db):
    """(gain in dB, whether a limiter is needed) to bring lufs to target.

    Boosts are capped at max_gain_db. When the boosted true peak would exceed
    ceiling_db the caller adds a limiter, so quiet but peaky sounds still reach
    the target instead of being held back by one transient.
    """
    if lufs is None:
        return 0.0, False
    gain = max(-40.0, min(max_gain_db, target - lufs))
    limiter = true_peak is not None and true_peak + gain > ceiling_db
    return round(gain, 2), limiter


def gain_filter(gain_db, limiter, ceiling_db):
    """ffmpeg -af value for a static gain, or None when nothing needs to change."""
    if abs(gain_db) < 0.1 and not limiter:
        return None
    parts = [f'volume={gain_db:.2f}dB']
    if limiter:
        # level=0: don't auto-raise the output to the limit afterwards.
        parts.append(f'alimiter=limit={10 ** (ceiling_db / 20):.4f}:level=0')
    return ','.join(parts)


# --- file names -----------------------------------------------------------

def sanitize_filename(raw, extensions):
    """A safe flat file name for an upload, keeping Unicode letters.
    Raises LibraryError for an extension that is not allowed."""
    name = unicodedata.normalize('NFC', str(raw or '')).replace('\\', '/').rsplit('/', 1)[-1]
    name = ''.join(c for c in name if c.isprintable() and c not in '<>:"|?*')
    stem, dot, ext = ' '.join(name.split()).rpartition('.')
    ext = ext.strip().lower() if dot else ''
    if ext not in extensions:
        raise LibraryError(f'file type .{ext or "?"} is not allowed (allowed: {", ".join(extensions)})', status=415)
    stem = stem.strip(' .')[:120].strip(' .') or 'sound'
    return f'{stem}.{ext}'


def is_visible_name(name, extensions):
    """A playable library entry: flat, not hidden, with an allowed extension."""
    return (bool(name) and '/' not in name and '\\' not in name and not name.startswith('.')
            and '.' in name and name.rsplit('.', 1)[-1].lower() in extensions)


# --- the library ----------------------------------------------------------

class SoundLibrary:
    def __init__(self, sounds_dir, db_path, extensions, *, normalize=True, target_lufs=-16.0,
                 max_gain_db=12.0, peak_ceiling_db=-1.0, scan_interval=300.0):
        """extensions: callable returning the allowed extensions (without dot)."""
        self.sounds_dir = sounds_dir
        self.db_path = db_path
        self._extensions = extensions
        self.normalize = normalize
        self.target_lufs = target_lufs
        self.max_gain_db = max_gain_db
        self.peak_ceiling_db = peak_ceiling_db
        self.scan_interval = scan_interval

        self._state_lock = threading.Lock()
        # Serializes everything that adds, renames or rewrites files.
        self.write_lock = threading.RLock()
        self._wake = threading.Event()
        self._files = {}     # name -> (size, mtime_ns, fingerprint or None)
        self._loudness = {}  # fingerprint -> (lufs, true_peak, error)
        self._thread = None
        self._init_db()

    # persistence

    def _db(self):
        return sqlite3.connect(self.db_path, timeout=10)

    def _init_db(self):
        with self._db() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS files (name TEXT PRIMARY KEY, size INTEGER, '
                         'mtime_ns INTEGER, fingerprint TEXT)')
            conn.execute('CREATE TABLE IF NOT EXISTS loudness (fingerprint TEXT PRIMARY KEY, lufs REAL, '
                         'true_peak REAL, error TEXT, analyzed_at REAL)')
            files = {row[0]: (row[1], row[2], row[3]) for row in conn.execute('SELECT * FROM files')}
            loudness = {row[0]: (row[1], row[2], row[3])
                        for row in conn.execute('SELECT fingerprint, lufs, true_peak, error FROM loudness')}
        with self._state_lock:
            self._files, self._loudness = files, loudness

    def _store_file(self, name, entry):
        with self._state_lock:
            self._files[name] = entry
        with self._db() as conn:
            conn.execute('INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)', (name, *entry))

    def _forget_files(self, names):
        if not names:
            return
        with self._state_lock:
            for name in names:
                self._files.pop(name, None)
        with self._db() as conn:
            conn.executemany('DELETE FROM files WHERE name = ?', [(n,) for n in names])

    def _store_loudness(self, fingerprint, lufs, peak, error):
        with self._state_lock:
            self._loudness[fingerprint] = (lufs, peak, error)
        with self._db() as conn:
            conn.execute('INSERT OR REPLACE INTO loudness VALUES (?, ?, ?, ?, ?)',
                         (fingerprint, lufs, peak, error, time.time()))

    # scanning

    def extensions(self):
        return tuple(e.strip().lower().lstrip('.') for e in self._extensions() if e.strip())

    def path(self, name):
        return os.path.join(self.sounds_dir, name)

    def visible_names(self):
        try:
            names = os.listdir(self.sounds_dir)
        except FileNotFoundError:
            return []
        extensions = self.extensions()
        return sorted(n for n in names if is_visible_name(n, extensions) and os.path.isfile(self.path(n)))

    @staticmethod
    def _stamp(path):
        st = os.stat(path)
        return st.st_size, st.st_mtime_ns

    def sync(self):
        """Fingerprint new or changed files and drop vanished ones. Cheap when
        nothing changed (one stat per file)."""
        names = self.visible_names()
        with self._state_lock:
            known = dict(self._files)
        for name in names:
            try:
                stamp = self._stamp(self.path(name))
            except OSError:
                continue
            entry = known.get(name)
            if entry and entry[:2] == stamp:
                continue
            fingerprint = audio_fingerprint(self.path(name))
            try:
                if self._stamp(self.path(name)) != stamp:
                    continue  # changed while hashing; next pass picks it up
            except OSError:
                continue
            self._store_file(name, (*stamp, fingerprint))
        self._forget_files(set(known) - set(names))

    def _pending_analysis(self):
        with self._state_lock:
            todo = {}
            for name, (_, _, fingerprint) in self._files.items():
                if fingerprint and fingerprint not in self._loudness and fingerprint not in todo:
                    todo[fingerprint] = name
        return todo

    def analyze_pending(self):
        """Measure loudness for every fingerprint that has none yet."""
        for fingerprint, name in self._pending_analysis().items():
            try:
                lufs, peak = measure_loudness(self.path(name))
                self._store_loudness(fingerprint, lufs, peak, None)
            except FileNotFoundError:
                continue
            except Exception as e:
                print(f"[LIBRARY] Loudness analysis of {name} failed: {e}")
                self._store_loudness(fingerprint, None, None, str(e)[:300])

    def start(self):
        """Background thread: initial scan, then rescans every scan_interval or on wake()."""
        if self._thread:
            return
        def loop():
            while True:
                try:
                    self.sync()
                    if self.normalize:
                        self.analyze_pending()
                except Exception as e:
                    print(f"[LIBRARY] Scan failed: {e}")
                self._wake.wait(self.scan_interval)
                self._wake.clear()
        self._thread = threading.Thread(target=loop, name='library-indexer', daemon=True)
        self._thread.start()

    def wake(self):
        self._wake.set()

    # lookups

    def entry(self, name):
        """Index record of one sound: fingerprint and loudness, or None if unknown."""
        with self._state_lock:
            file_entry = self._files.get(name)
            if not file_entry:
                return None
            loudness = self._loudness.get(file_entry[2]) if file_entry[2] else None
        return file_entry, loudness

    def loudness_info(self, name):
        """Public normalization status of a sound for /meta."""
        found = self.entry(name)
        if not found:
            return {'status': 'pending', 'lufs': None, 'true_peak': None, 'gain_db': None}
        (_, _, fingerprint), loudness = found
        if not fingerprint:
            return {'status': 'unreadable', 'lufs': None, 'true_peak': None, 'gain_db': None}
        if not loudness:
            return {'status': 'pending', 'lufs': None, 'true_peak': None, 'gain_db': None}
        lufs, peak, error = loudness
        if error:
            return {'status': 'failed', 'lufs': None, 'true_peak': None, 'gain_db': None}
        gain, limiter = normalization_gain(lufs, peak, self.target_lufs, self.max_gain_db, self.peak_ceiling_db)
        return {
            'status': 'silent' if lufs is None else 'analyzed',
            'lufs': None if lufs is None else round(lufs, 1),
            'true_peak': None if peak is None else round(peak, 1),
            'gain_db': gain if self.normalize else 0.0,
            'limiter': limiter and self.normalize,
        }

    def playback_filter(self, name):
        """ffmpeg -af chain that normalizes this sound, or None (not analyzed
        yet, normalization off, or no change needed)."""
        if not self.normalize:
            return None
        found = self.entry(name)
        if not found:
            self.wake()
            return None
        (size, mtime_ns, fingerprint), loudness = found
        try:
            if self._stamp(self.path(name)) != (size, mtime_ns):
                self.wake()  # changed on disk; the stored analysis may be outdated
                return None
        except OSError:
            return None
        if not loudness or loudness[2]:
            return None
        gain, limiter = normalization_gain(loudness[0], loudness[1], self.target_lufs,
                                           self.max_gain_db, self.peak_ceiling_db)
        return gain_filter(gain, limiter, self.peak_ceiling_db)

    def find_by_fingerprint(self, fingerprint, exclude=None):
        with self._state_lock:
            for name, (_, _, fp) in sorted(self._files.items()):
                if fp == fingerprint and name != exclude and os.path.exists(self.path(name)):
                    return name
        return None

    # mutations

    def _free_name(self, name):
        if not os.path.lexists(self.path(name)):
            return name
        stem, ext = os.path.splitext(name)
        for i in range(2, 1000):
            candidate = f'{stem} ({i}){ext}'
            if not os.path.lexists(self.path(candidate)):
                return candidate
        raise LibraryError('too many files with this name', status=409)

    def add_upload(self, stream, filename, max_bytes):
        """Store an uploaded file. Returns its final name. Raises DuplicateError
        when the same audio already exists (details: existing=name), or
        LibraryError for anything that is not a playable, allowed audio file.
        An existing different file with the same name gets a ' (2)' suffix."""
        name = sanitize_filename(filename, self.extensions())
        fd, tmp = tempfile.mkstemp(prefix=UPLOAD_PREFIX, suffix=os.path.splitext(name)[1], dir=self.sounds_dir)
        try:
            written = 0
            with os.fdopen(fd, 'wb') as out:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise LibraryError(f'file is larger than {max_bytes // (1024 * 1024)} MB', status=413)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            if not written:
                raise LibraryError('empty file')
            fingerprint = audio_fingerprint(tmp)
            if not fingerprint or not decodes_cleanly(tmp):
                raise LibraryError('not a playable audio file', status=415)
            os.chmod(tmp, 0o644)
            with self.write_lock:
                self.sync()  # include files dropped into the folder by hand
                existing = self.find_by_fingerprint(fingerprint)
                if existing:
                    raise DuplicateError(f'same audio as "{existing}"', existing=existing)
                name = self._free_name(name)
                os.rename(tmp, self.path(name))
                self._store_file(name, (*self._stamp(self.path(name)), fingerprint))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self.wake()  # analyze loudness in the background
        return name

    def delete(self, name):
        """Mask a sound by renaming it; returns the new (hidden) name."""
        with self.write_lock:
            if not is_visible_name(name, self.extensions()) or not os.path.isfile(self.path(name)):
                raise LibraryError('sound not found', status=404)
            stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
            masked = f'{name}{DELETED_MARK}{stamp}'
            counter = 1
            while os.path.lexists(self.path(masked)):
                counter += 1  # deleted twice within one second
                masked = f'{name}{DELETED_MARK}{stamp}-{counter}'
            os.rename(self.path(name), self.path(masked))
            self._forget_files([name])
        return masked

    def deleted(self):
        """Masked sounds, newest first: [{'id', 'name', 'deleted_at'}]."""
        try:
            names = os.listdir(self.sounds_dir)
        except FileNotFoundError:
            return []
        items = []
        for entry in names:
            match = DELETED_PATTERN.match(entry)
            if match and not entry.startswith('.') and os.path.isfile(self.path(entry)):
                stamp = match.group('stamp')
                items.append({
                    'id': entry,
                    'name': match.group('name'),
                    'deleted_at': f'{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}Z',
                })
        items.sort(key=lambda item: (item['deleted_at'], item['id']), reverse=True)
        return items

    def restore(self, masked):
        """Undo a delete. Refuses if the name is taken or the audio was re-added meanwhile."""
        with self.write_lock:
            match = DELETED_PATTERN.match(masked)
            if not match or '/' in masked or masked.startswith('.') or not os.path.isfile(self.path(masked)):
                raise LibraryError('deleted sound not found', status=404)
            name = match.group('name')
            if not is_visible_name(name, self.extensions()):
                raise LibraryError('file type is no longer allowed', status=409)
            if os.path.lexists(self.path(name)):
                raise LibraryError(f'"{name}" exists again; delete or rename it first', status=409)
            self.sync()
            fingerprint = audio_fingerprint(self.path(masked))
            existing = fingerprint and self.find_by_fingerprint(fingerprint)
            if existing:
                raise DuplicateError(f'same audio as "{existing}"', existing=existing)
            os.rename(self.path(masked), self.path(name))
            self._store_file(name, (*self._stamp(self.path(name)), fingerprint))
        self.wake()
        return name

    def file_changed(self, name):
        """Re-index one sound after its tags were rewritten."""
        try:
            stamp = self._stamp(self.path(name))
        except OSError:
            return
        self._store_file(name, (*stamp, audio_fingerprint(self.path(name))))
