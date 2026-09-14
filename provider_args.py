"""Parse user-entered provider arguments without shell evaluation."""
import shlex


def validate_provider_args(value):
    if not isinstance(value, list) or any(not isinstance(arg, str) or '\0' in arg for arg in value):
        raise ValueError('Provider flags must be a list of strings without NUL characters')
    return list(value)


def parse_provider_flags(text):
    if not isinstance(text, str):
        raise ValueError('Provider flags must be text')
    try:
        return validate_provider_args(shlex.split(text))
    except ValueError as error:
        raise ValueError(f'Invalid provider flags: {error}') from None
