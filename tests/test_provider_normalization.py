import pytest
from app.providers.the_odds_api import TheOddsAPIProvider, ProviderError

def test_provider_requires_key():
    with pytest.raises(ProviderError):
        TheOddsAPIProvider("")
