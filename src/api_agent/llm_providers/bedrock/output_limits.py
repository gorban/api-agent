"""Conservative recognition of explicit Bedrock output-parameter rejections."""
import re


_FIELD = r'(?:max_output_tokens|max_tokens|maxOutputTokens|maxTokens)'
_PATTERNS = (
    rf"{_FIELD} must be between 1 and ([0-9]+)(?:\.)?",
    rf"{_FIELD}: [0-9]+ is greater than the maximum of ([0-9]+)",
    rf"Invalid '({_FIELD})': integer above maximum value\. Expected a value <= ([0-9]+), but got [0-9]+ instead\.",
    rf"(?:1 validation error detected: )?Value '[0-9]+' at '(?:inferenceConfig\.)?{_FIELD}' failed to satisfy constraint: Member must have value less than or equal to ([0-9]+)",
)


def rejected_maximum(status, detail):
    """Return only an explicit positive maximum from a whole validation message.

    HTTP validation errors describe request admission, whereas context-window,
    quota, streaming and server failures do not establish an output maximum.
    Callers must separately establish that no inference response was observed.
    """
    if type(status) is not int or status not in (400, 422) or type(detail) is not dict:
        return None
    if any(key in detail for key in ('usage', 'output', 'choices', 'response')):
        return None
    message = detail.get('message', detail.get('Message'))
    if type(message) is not str or len(message) > 512:
        return None
    for pattern in _PATTERNS:
        matched = re.fullmatch(pattern, message)
        if matched:
            maximum = int(matched.groups()[-1])
            return maximum if 0 < maximum <= 1048576 else None
    return None
