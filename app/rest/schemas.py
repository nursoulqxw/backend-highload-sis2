from pydantic import BaseModel, Field

MAX_BATCH = 1000


class PostCounterOut(BaseModel):
    post_id: int
    likes: int
    views: int
    liked_by_me: bool


class ViewIn(BaseModel):
    user_id: int = Field(gt=0)
    post_id: int = Field(gt=0)


class RecordViewsIn(BaseModel):
    views: list[ViewIn] = Field(min_length=1, max_length=MAX_BATCH)


class RecordViewsOut(BaseModel):
    counted: int
