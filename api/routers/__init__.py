from .feeds import router as feeds_router
from .well_known import router as well_known_router
from .feed_generator import router as feed_generator_router
from .interactions import router as interactions_router
from .status import router as status_router

__all__ = ["feeds_router", "well_known_router", "feed_generator_router", "interactions_router", "status_router"]
