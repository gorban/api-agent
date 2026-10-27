"""Combine independently configured predictions without pooling test profiles."""
from api_agent.telemetry.estimation import EstimateSnapshot, key


class ProfileEstimator(EstimateSnapshot):
    def __init__(self, children):
        self.children = tuple(children)
        predictions, records, overlaps = {}, {}, {}
        first = self.children[0]
        card = first.card
        for child in self.children:
            predictions.update(child.predictions)
            records.update((key(row), row) for row in child.records)
            overlaps.update((key(row), row) for row in child.overlap_records or ())
            if child.card != card:
                card = None
        super().__init__(predictions, records=list(records.values()), card=card,
                         overlap_records=list(overlaps.values()), now=first.now,
                         ignored_settings=first.ignored_settings)

    def synchronize(self):
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
