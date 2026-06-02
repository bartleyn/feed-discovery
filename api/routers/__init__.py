from .feeds import router as feeds_router
from .well_known import router as well_known_router
from .feed_generator import router as feed_generator_router

__all__ = ["feeds_router", "well_known_router", "feed_generator_router"]
