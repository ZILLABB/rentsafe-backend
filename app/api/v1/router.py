"""Aggregates all v1 routers under /api/v1."""

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    agents,
    alerts,
    auth,
    commute,
    fees,
    neighbourhoods,
    photos,
    places,
    properties,
    pushapi,
    responses,
    reviews,
    saved,
    users,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(properties.router)
api_router.include_router(reviews.router)
api_router.include_router(responses.router)
api_router.include_router(commute.router)
api_router.include_router(places.router)
api_router.include_router(fees.router)
api_router.include_router(photos.router)
api_router.include_router(saved.router)
api_router.include_router(agents.router)
api_router.include_router(neighbourhoods.router)
api_router.include_router(alerts.router)
api_router.include_router(alerts.watch_router)
api_router.include_router(pushapi.router)
api_router.include_router(admin.router)
