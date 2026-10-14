"""Restricted validator for the shared hosted-flash contract constants.

No remote schema, production key, USB, or third-party schema dependency.
"""
import base64
import json
from pathlib import Path
import re
import unicodedata

CONTRACT = json.loads((Path(__file__).resolve().parent / 'assets/flash-images/contract-v1.json').read_bytes())

def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result

    def reject(value):
        raise ValueError('float or nonfinite JSON value forbidden')

    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_float=reject, parse_constant=reject)
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError('invalid bounded UTF-8 JSON') from exc


def check_schema(value, schema, path='$', *, origin=None):
    origin = CONTRACT['origin'] if origin is None else origin
    if '$ref' in schema:
        return check_schema(value, CONTRACT['definitions'][schema['$ref'].split('/')[-1]], path, origin=origin)
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        raise ValueError(path + ': wrong constant')
    if 'enum' in schema and not any(type(value) is type(v) and value == v for v in schema['enum']):
        raise ValueError(path + ': unsupported value')
    expected = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'boolean': bool}
    if expected and type(value) is not types[expected]:
        raise ValueError(path + ': wrong type')
    if isinstance(value, str):
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 4096):
            raise ValueError(path + ': string bounds')
        if any(unicodedata.category(c).startswith('C') for c in value) or 'PLACEHOLDER_' in value:
            raise ValueError(path + ': control/placeholder forbidden')
        if 'pattern' in schema and not re.fullmatch(schema['pattern'], value, flags=re.ASCII):
            raise ValueError(path + ': pattern')
        fmt = schema.get('format')
        if fmt in ('base64', 'ed25519-signature'):
            try:
                raw = base64.b64decode(value, validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError(path + ': invalid base64') from exc
            if base64.b64encode(raw).decode() != value or (fmt == 'ed25519-signature' and len(raw) != 64):
                raise ValueError(path + ': noncanonical base64/signature')
        if fmt == 'image-url' and not re.fullmatch(re.escape(origin) + r'/flash/images/[a-z0-9][a-z0-9.-]{0,95}/[0-9a-f]{64}\.tar\.bz2', value):
            raise ValueError(path + ': unsafe image URL')
        if fmt == 'manifest-url' and not re.fullmatch(re.escape(origin) + r'/flash/manifests/[a-z0-9][a-z0-9.-]{0,95}\.json', value):
            raise ValueError(path + ': unsafe manifest URL')
        if fmt == 'member-name' and not re.fullmatch(r'phoenix-[a-z0-9][a-z0-9.-]{0,95}/flash_jibo/output/images/(rootfs|services|skills|var)\.ext4', value):
            raise ValueError(path + ': unsafe member name')
    if type(value) is int and not schema.get('minimum', value) <= value <= schema.get('maximum', value):
        raise ValueError(path + ': integer bounds')
    if isinstance(value, list):
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 128):
            raise ValueError(path + ': array bounds')
        if schema.get('uniqueItems') and len({canonical_json(v) for v in value}) != len(value):
            raise ValueError(path + ': duplicate items')
        for i, item in enumerate(value):
            check_schema(item, schema['items'], path + '[' + str(i) + ']', origin=origin)
    if isinstance(value, dict):
        if not schema.get('minProperties', 0) <= len(value) <= schema.get('maxProperties', 128):
            raise ValueError(path + ': property bounds')
        props = schema.get('properties', {})
        if not set(schema.get('required', [])).issubset(value):
            raise ValueError(path + ': missing fields')
        for key, item in value.items():
            if 'propertyNames' in schema:
                check_schema(key, schema['propertyNames'], path + '.key', origin=origin)
            if key in props:
                check_schema(item, props[key], path + '.' + key, origin=origin)
            elif schema.get('additionalProperties') is False:
                raise ValueError(path + ': unexpected field')
            elif isinstance(schema.get('additionalProperties'), dict):
                check_schema(item, schema['additionalProperties'], path + '.' + key, origin=origin)


def validate_shape(payload, document, *, origin=None):
    if document not in CONTRACT['fields']:
        raise ValueError('unknown document kind')
    check_schema(payload, {'type': 'object', 'properties': CONTRACT['fields'][document],
                           'required': list(CONTRACT['fields'][document]), 'additionalProperties': False},
                 origin=origin)
    return payload
