from pydantic import BaseModel, ConfigDict, Field, StrictStr


class TwseResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    stat: StrictStr
    date: StrictStr | None = None
    title: StrictStr | None = None
    fields: list[StrictStr] = Field(default_factory=list)
    data: list[list[StrictStr]] = Field(default_factory=list)
