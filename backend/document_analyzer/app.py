from dotenv import load_dotenv

load_dotenv()  # must run before any of these import env-reading modules (e.g. resource_bounds.py)

from fastapi import FastAPI

from .agent2.router import router as agent2_router
from .agent3.router import router as agent3_router
from .config_router import router as config_router
from .jobs import router as jobs_router
from .projects_router import router as projects_router


def create_app() -> FastAPI:
    app = FastAPI(title="Galvanise")
    app.include_router(config_router)
    app.include_router(jobs_router)
    app.include_router(projects_router)
    app.include_router(agent2_router)
    app.include_router(agent3_router)
    return app


app = create_app()
