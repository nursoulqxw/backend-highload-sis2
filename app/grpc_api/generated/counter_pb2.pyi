from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Empty(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class LikeRequest(_message.Message):
    __slots__ = ("user_id", "post_id")
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    POST_ID_FIELD_NUMBER: _ClassVar[int]
    user_id: int
    post_id: int
    def __init__(self, user_id: _Optional[int] = ..., post_id: _Optional[int] = ...) -> None: ...

class ViewRequest(_message.Message):
    __slots__ = ("user_id", "post_id")
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    POST_ID_FIELD_NUMBER: _ClassVar[int]
    user_id: int
    post_id: int
    def __init__(self, user_id: _Optional[int] = ..., post_id: _Optional[int] = ...) -> None: ...

class GetCountersRequest(_message.Message):
    __slots__ = ("user_id", "post_ids")
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    POST_IDS_FIELD_NUMBER: _ClassVar[int]
    user_id: int
    post_ids: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, user_id: _Optional[int] = ..., post_ids: _Optional[_Iterable[int]] = ...) -> None: ...

class PostCounter(_message.Message):
    __slots__ = ("post_id", "likes", "views", "liked_by_me")
    POST_ID_FIELD_NUMBER: _ClassVar[int]
    LIKES_FIELD_NUMBER: _ClassVar[int]
    VIEWS_FIELD_NUMBER: _ClassVar[int]
    LIKED_BY_ME_FIELD_NUMBER: _ClassVar[int]
    post_id: int
    likes: int
    views: int
    liked_by_me: bool
    def __init__(self, post_id: _Optional[int] = ..., likes: _Optional[int] = ..., views: _Optional[int] = ..., liked_by_me: _Optional[bool] = ...) -> None: ...

class GetCountersResponse(_message.Message):
    __slots__ = ("counters",)
    COUNTERS_FIELD_NUMBER: _ClassVar[int]
    counters: _containers.RepeatedCompositeFieldContainer[PostCounter]
    def __init__(self, counters: _Optional[_Iterable[_Union[PostCounter, _Mapping]]] = ...) -> None: ...
