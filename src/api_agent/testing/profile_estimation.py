"""Combine independently configured predictions without pooling test profiles."""
from contextlib import nullcontext

from .history_projection import HistoryProjection, ProjectionCancelled, check_projection, encode_history
from api_agent.telemetry.estimation import EstimateSnapshot, key


class ProfileEstimator(EstimateSnapshot):
    def __init__(self, children):
        children_read = []
        try:
            for child in children:
                children_read.append(child)
                check_projection()
            self.children = tuple(children_read)
            self._compose()
        except ProjectionCancelled:
            for child in children_read:
                child.close()
            raise

    def _compose(self):
        predictions, records, overlaps = {}, {}, {}
        first = self.children[0]
        card = first.card
        for child in self.children:
            predictions.update(child.predictions)
            for row in child.records:
                check_projection()
                records[key(row)] = row
            for row in child.overlap_records or ():
                check_projection()
                overlaps[key(row)] = row
            if child.card != card:
                card = None
        cache = getattr(first, 'cache', None)
        if isinstance(cache, HistoryProjection) and all(getattr(child, 'cache', None) is cache for child in self.children):
            super().__init__(predictions, card=card, now=first.now, ignored_settings=first.ignored_settings)
            self._set_history_json(encode_history(list(records.values())), encode_history(list(overlaps.values())))
        else:
            super().__init__(predictions, records=list(records.values()), card=card,
                             overlap_records=list(overlaps.values()), now=first.now,
                             ignored_settings=first.ignored_settings)

    def synchronize(self):
        cache = getattr(self.children[0], 'cache', None)
        shared = isinstance(cache, HistoryProjection) and all(
            getattr(child, 'cache', None) is cache for child in self.children)
        with cache.read() if shared else nullcontext():
            return ProfileEstimator(child.synchronize() for child in self.children)

    def cancel_synchronization(self):
        for child in self.children:
            cancel = getattr(child, 'cancel_synchronization', None)
            if cancel is not None:
                cancel()

    def ingest(self, directory, manifest):
        for child in self.children:
            child.ingest(directory, manifest)

    def close(self):
        for child in self.children:
            child.close()
