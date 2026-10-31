"""GPT-owned recognition of explicit output parameter validation failures."""
import re

from api_agent.providers.output_limits import OutputLimitRejected


def output_rejection(status, data, *, subscription, api_mode):
    """Normalize a strict HTTP validation envelope without releasing its text."""
    if (subscription or type(status) is not int or status not in (400, 422)
            or type(data) is not dict or set(data) != {'error'}):
        return None
    detail = data['error']
    field = 'max_output_tokens' if api_mode == 'responses' else 'max_completion_tokens'
    if (type(detail) is not dict or not set(detail) <= {'message', 'param', 'code', 'type'}
            or detail.get('type') != 'invalid_request_error'
            or detail.get('code') not in (None, 'integer_above_max_value')
            or detail.get('param') != field):
        return None
    message = detail.get('message')
    if type(message) is not str or len(message) > 512:
        return None
    patterns = (
        rf"Invalid '{field}': integer above maximum value\. Expected a value <= ([0-9]+), but got [0-9]+ instead\.",
        rf'{field}: [0-9]+ is greater than the maximum of ([0-9]+)',
        rf'{field} must be between 1 and ([0-9]+)\.?',
    )
    for pattern in patterns:
        matched = re.fullmatch(pattern, message)
        if matched:
            maximum = int(matched.group(1))
            if 0 < maximum <= 1048576:
                return OutputLimitRejected(maximum, status=status)
            return None
    return None
