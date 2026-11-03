"""Numeric GPT generations, independent of display labels and catalog identity."""
import re
from packaging.version import Version


def gpt_version(model):
    if type(model) is not str or len(model) > 200:
        return None
    match = re.fullmatch(r'gpt-((?:0|[1-9][0-9]*)(?:[.](?:0|[1-9][0-9]*))*)(?:-([a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)*))?', model)
    if match is None:
        return None
    return Version(match[1]), match[2]
