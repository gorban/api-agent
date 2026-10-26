"""Await evidence for each explicitly identified UI response before the next turn."""
from contextlib import asynccontextmanager
from uuid import UUID


class UITurn:
    """Bind only identifiers observed on the application's submitted request."""
    def __init__(self, boundary=None):
        self.boundary = boundary
        self._owner = None

    @property
    def owner(self):
        return None if self._owner is None else dict(self._owner)

    def bind(self, *, conversation_id, turn_id):
        for value in (conversation_id, turn_id):
            if type(value) is not str or str(UUID(value)) != value:
                raise ValueError('UI turn identifiers must be canonical UUIDs')
        owner = {'kind': 'turn', 'conversation_id': conversation_id, 'turn_id': turn_id}
        if self._owner is not None or (self.boundary is not None
                and self.boundary.content_export(owner) != owner):
            raise ValueError('Invalid UI turn ownership')
        self._owner = owner


@asynccontextmanager
async def ui_turn(prepared):
    """Serialize prompt, render and awaited capture on the fixture's owner."""
    evidence = prepared.services.get('ui_turn_evidence')
    if evidence is None:
        yield UITurn()
        return
    async with evidence.ui_turn_lock:
        turn = UITurn(evidence.emissions)
        phase = 'failure'
        primary = None
        try:
            yield turn
            phase = 'success'
        except BaseException as error:
            primary = error
            raise
        finally:
            if turn.owner is not None:
                try:
                    await evidence.capture(phase, owner=dict(turn.owner))
                except BaseException as error:
                    if primary is None and not isinstance(error, Exception):
                        raise
