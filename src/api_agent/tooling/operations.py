"""Operation vocabularies for tool interfaces.

``verified-read@1`` keeps its fixed four-operation vocabulary, all idempotent:
the interface is read-only, so any implementation of it must not write. ``operations@1``
lets an implementation declare its own named operations, each optionally marked
idempotent, which nodes then select by name. An operation that is not declared
idempotent is non-idempotent: a node that called it is never replayed.
"""
import keyword
import re

from api_agent.workflows.model import LayoutError

OPERATIONS_INTERFACE = 'operations@1'
FIXED_OPERATIONS = ('describe', 'prepare', 'verify', 'execute')
FIXED_IDEMPOTENT = frozenset(FIXED_OPERATIONS)
_NAME = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
# Owner and dispatch method names can never double as declared operations.
_RESERVED = frozenset((*FIXED_OPERATIONS, 'invoke', 'aclose', 'astart', 'for_invocation'))


def name_problem(name):
    """Return why a name cannot be an implementation-declared operation, else None."""
    if type(name) is not str or not _NAME.fullmatch(name) or '__' in name:
        return 'invalid operation name'
    if name in _RESERVED or keyword.iskeyword(name):
        return 'reserved operation name'
    return None


def declared_operations(data):
    """Map each declared operation name to whether it is idempotent for an operations@1 declaration."""
    if data.get('interface') != OPERATIONS_INTERFACE:
        return {}
    return {name: row.get('idempotent', False) for name, row in data['operations'].items()}


def check_use(use, interface, declared, implementation):
    """Reject node Operations that the selected interface or implementation does not offer."""
    if use.scope_kind != 'node':
        return
    where = f'{use.declaration}:{use.line}: '
    if interface != OPERATIONS_INTERFACE:
        for operation in use.operations:
            if operation not in FIXED_OPERATIONS:
                raise LayoutError(f"{where}operation '{operation}' is not supported by {interface}; "
                                  'use ' + ', '.join(FIXED_OPERATIONS))
        return
    if not use.operations:
        raise LayoutError(f'{where}node tool {use.port} must list Operations declared by {implementation}')
    for operation in use.operations:
        if operation not in declared:
            raise LayoutError(f"{where}operation '{operation}' is not declared by {implementation} "
                              '(declared: ' + ', '.join(sorted(declared)) + ')')
