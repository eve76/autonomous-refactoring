"""Stagnation-based stopping criterion.

Counter increments when:
  - a merge yields penalty reduction below `min_merge_gain`, OR
  - a programmer is killed for exceeding its time limit.

A merge above `min_merge_gain` resets the counter to zero.
The system halts when the counter reaches `stagnation_limit`.
"""

from dataclasses import dataclass


@dataclass
class StagnationTracker:
    min_merge_gain: float
    limit: int
    counter: int = 0

    def record_merge(self, penalty_reduction: float) -> None:
        if penalty_reduction > self.min_merge_gain:
            self.counter = 0
        else:
            self.counter += 1

    def record_timeout(self) -> None:
        self.counter += 1

    def should_stop(self) -> bool:
        return self.counter >= self.limit
