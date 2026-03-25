from pydantic import BaseModel, AnyUrl
from enum import Enum
from datetime import datetime

class ConditionEnum(str, Enum):
    new = 'new'
    used = 'used'
    refurbished = 'redfurbished'
    open_box = 'open box'

class CurrencyEnum(str, Enum):
    CAD = 'CAD'
    USD = 'USD'
    other = 'OTHER'

class Listing(BaseModel):
    name: str
    price: float
    description: str
    source_name: str
    source_url: AnyUrl
    condition: ConditionEnum
    currency: CurrencyEnum
    found_at: datetime

