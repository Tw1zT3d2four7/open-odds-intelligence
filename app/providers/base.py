from abc import ABC, abstractmethod

class OddsProvider(ABC):
    name = "base"
    @abstractmethod
    def fetch_odds(self, sport: str) -> list[dict]:
        raise NotImplementedError
