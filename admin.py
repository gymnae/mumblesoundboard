"""The /edit area: upload, tag, cover and (masked) delete sounds.

Everything under /edit is meant to sit behind a caddy-security authorization
policy; admin_auth.AdminAuth checks what the proxy passes on.
"""
import io
import os

from flask import Blueprint, jsonify, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

import cover_image
import tag_writer
from library import LibraryError, is_visible_name


URL_PREFIX = '/edit'


def create_admin_blueprint(library, auth, sound_info, max_upload_bytes):
    """sound_info(name) -> public record of a sound (as in /meta) or None."""
    admin = Blueprint('admin', __name__, url_prefix=URL_PREFIX)

    @admin.before_request
    def require_admin():
        result = auth.check(request)
        if result.ok:
            request.admin_user = result.user
            return None
        if request.path.startswith(URL_PREFIX + '/api/'):
            return jsonify({'error': result.error}), result.status
        return render_template('admin_denied.html', status=result.status, error=result.error), result.status

    def error(message, status=400, **extra):
        return jsonify(dict(extra, error=message)), status

    def visible(name):
        return is_visible_name(name, library.extensions()) and os.path.isfile(library.path(name))

    def info_or_404(name):
        info = sound_info(name) if visible(name) else None
        if not info:
            return None, error('sound not found', 404)
        return info, None

    @admin.errorhandler(LibraryError)
    def library_error(e):
        return error(str(e), e.status, **e.details)

    @admin.errorhandler(tag_writer.TagWriteError)
    def tag_error(e):
        return error(str(e), 400)

    @admin.errorhandler(cover_image.ImageError)
    def image_error(e):
        return error(str(e), 400)

    @admin.errorhandler(RequestEntityTooLarge)
    def too_large(e):
        return error(f'upload is larger than {max_upload_bytes // (1024 * 1024)} MB', 413)

    @admin.route('/')
    @admin.route('')
    def page():
        return render_template('admin.html', user=request.admin_user,
                               max_upload_mb=max_upload_bytes // (1024 * 1024),
                               extensions=library.extensions(),
                               api_base=URL_PREFIX + '/api/',
                               cover_accept=cover_image.ACCEPT,
                               cover_max_px=cover_image.COVER_MAX_PX,
                               cover_max_bytes=cover_image.MAX_INPUT_BYTES,
                               cover_formats=cover_image.FORMAT_NAMES)

    @admin.route('/api/me')
    def me():
        return jsonify({
            'user': request.admin_user,
            'jwt_verified': auth.verifies_jwt,
            'max_upload_bytes': max_upload_bytes,
            'extensions': list(library.extensions()),
            'cover': {
                'aspect': cover_image.COVER_ASPECT,
                'max_px': cover_image.COVER_MAX_PX,
                'max_bytes': cover_image.MAX_INPUT_BYTES,
                'formats': list(cover_image.FORMAT_NAMES),
            },
            'normalization': {
                'enabled': library.normalize,
                'target_lufs': library.target_lufs,
                'max_gain_db': library.max_gain_db,
                'peak_ceiling_db': library.peak_ceiling_db,
            },
        })

    @admin.route('/api/upload', methods=['POST'])
    def upload():
        upload_file = request.files.get('file')
        if not upload_file or not upload_file.filename:
            return error('no file in the "file" form field')
        name = library.add_upload(upload_file.stream, upload_file.filename, max_upload_bytes)
        print(f"[ADMIN] {request.admin_user} uploaded {name}")
        return jsonify({'name': name, 'sound': sound_info(name)}), 201

    @admin.route('/api/sounds/<path:name>', methods=['PATCH'])
    def edit(name):
        fields = tag_writer.validate_fields(request.get_json(silent=True))
        with library.write_lock:
            info, missing = info_or_404(name)
            if missing:
                return missing
            tag_writer.write_fields(library.path(name), fields)
            library.file_changed(name)
        print(f"[ADMIN] {request.admin_user} edited tags of {name}")
        return jsonify({'sound': sound_info(name)})

    def uploaded_image(field):
        image = request.files.get(field)
        if not image:
            raise cover_image.ImageError(f'no image in the "{field}" form field')
        return image.read(cover_image.MAX_INPUT_BYTES + 1)

    @admin.route('/api/image-preview', methods=['POST'])
    def image_preview():
        """Any supported image as JPEG/PNG the browser can show in the cropper.
        X-Image-Width/-Height give the size of the upright original."""
        mime, data, width, height = cover_image.preview(uploaded_image('image'))
        response = send_file(io.BytesIO(data), mimetype=mime, max_age=0)
        response.headers['X-Image-Width'] = str(width)
        response.headers['X-Image-Height'] = str(height)
        return response

    @admin.route('/api/sounds/<path:name>/cover', methods=['PUT', 'POST', 'DELETE'])
    def cover(name):
        """multipart 'cover' (any supported image) and optional 'crop' = x,y,w,h
        as fractions of the upright image; the crop is made square."""
        data = crop = None
        if request.method != 'DELETE':
            data = uploaded_image('cover')
            crop = cover_image.parse_crop(request.form.get('crop'))
        with library.write_lock:
            info, missing = info_or_404(name)
            if missing:
                return missing
            tag_writer.write_cover(library.path(name), data, crop)
            library.file_changed(name)
        action = 'removed the cover of' if data is None else 'set a new cover for'
        print(f"[ADMIN] {request.admin_user} {action} {name}")
        return jsonify({'sound': sound_info(name)})

    @admin.route('/api/sounds/<path:name>', methods=['DELETE'])
    def delete(name):
        masked = library.delete(name)
        print(f"[ADMIN] {request.admin_user} deleted {name} (kept as {masked})")
        return jsonify({'deleted': name, 'id': masked})

    @admin.route('/api/trash')
    def trash():
        return jsonify(library.deleted())

    @admin.route('/api/trash/<path:masked>/restore', methods=['POST'])
    def restore(masked):
        name = library.restore(masked)
        print(f"[ADMIN] {request.admin_user} restored {name}")
        return jsonify({'name': name, 'sound': sound_info(name)})

    return admin
