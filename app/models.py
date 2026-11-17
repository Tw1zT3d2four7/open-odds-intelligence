from pydantic import BaseModel, Field
from typing import Optional

class Outcome(BaseModel):
    name: str
    price: int
    point: Optional[float] = None

class Market(BaseModel):
    key: str
    outcomes: list[Outcome] = Field(default_factory=list)

class Bookmaker(BaseModel):
    key: str
    title: str
    last_update: Optional[str] = None
    markets: list[Market] = Field(default_factory=list)

class Event(BaseModel):
    id: str
    sport_key: str
    sport_title: str = ""
    commence_time: str
    home_team: str
    away_team: str
    bookmakers: list[Bookmaker] = Field(default_factory=list)
    source: str = "unknown"
    retrieved_at: Optional[str] = None
