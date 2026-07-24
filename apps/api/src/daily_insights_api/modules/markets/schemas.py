from pydantic import BaseModel


class MarketResponse(BaseModel):
    code: str
    name_en: str
    name_zh_hant: str
    name_zh_hans: str
    is_visible: bool
