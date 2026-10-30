from pydantic import BaseModel


class PostCounterOut(BaseModel):
    post_id: int
    likes: int
    views: int
    liked_by_me: bool
