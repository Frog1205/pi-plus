"""Authenticated task routes for Atlas's standard-library HTTP server."""
import json
from .host import TaskError


def handle_tasks(handler, method, path):
    if path != '/api/v1/tasks' and not path.startswith('/api/v1/tasks/'):
        return False
    host = getattr(handler.server, 'task_host', None)
    try:
        if host is None:
            raise TaskError('controlled_runtime_disabled', 503)
        if not host.authenticate(handler.headers.get('Authorization')):
            raise TaskError('unauthorized', 401)
        # Bound parsing and reject duplicate JSON fields, NaN and unknown route/body fields.
        body = None
        if method == 'POST':
            if handler.headers.get('Transfer-Encoding'):
                raise TaskError('unsupported_transfer_encoding')
            size = int(handler.headers.get('Content-Length', '0'))
            if not 0 <= size <= 16384:
                raise TaskError('request_too_large', 413)
            def fields(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise TaskError('duplicate_json_field')
                    value[key] = item
                return value
            def invalid_constant(_):
                raise TaskError('invalid_json_number')
            body = json.loads(handler.rfile.read(size) or b'{}', object_pairs_hook=fields, parse_constant=invalid_constant)
        parts = path.removeprefix('/api/v1/tasks').strip('/').split('/')
        if parts == ['']:
            result = host.list() if method == 'GET' else host.submit(body)
        elif len(parts) == 1 and method == 'GET':
            result = host.get(parts[0])
        elif len(parts) == 2 and method == 'GET' and parts[1] in ('events','operations','artifact'):
            result = getattr(host, parts[1])(parts[0])
        elif len(parts) == 2 and method == 'POST' and parts[1] in ('cancel','resume'):
            if body != {}:
                raise TaskError('invalid_request_fields')
            result = getattr(host, parts[1])(parts[0])
        else:
            raise TaskError('not_found', 404)
        handler._json(result, 202 if method == 'POST' else 200)
    except TaskError as error:
        handler._json({'error':error.code}, error.status)
    except (ValueError, UnicodeError, RecursionError):
        handler._json({'error':'invalid_json_request'}, 400)
    return True
